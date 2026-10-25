// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title AtomicFrictionVault
/// @notice Settles the DEX leg of a DEX/CEX-perp basis trade atomically. Every route
///         (own capital or ERC-3156 flash liquidity) is wrapped in a balance-delta guard:
///         the tx reverts unless quote balance grows by >= max(minNetProfit, minProfitBps * notional).
///         Off-chain `friction_radar.spread_model` sizes the trade and supplies minNetProfit.

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function approve(address, uint256) external returns (bool);
}

interface IERC3156FlashLender {
    function flashLoan(address receiver, address token, uint256 amount, bytes calldata data) external returns (bool);
}

interface IERC3156FlashBorrower {
    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external returns (bytes32);
}

contract AtomicFrictionVault is IERC3156FlashBorrower {
    struct Leg {
        address target;    // whitelisted router / pool
        address approveToken; // token to approve before call (0 = none)
        uint256 approveAmount;
        bytes data;        // encoded swap call
    }

    bytes32 public constant CALLBACK_SUCCESS = keccak256("ERC3156FlashBorrower.onFlashLoan");
    uint16 public constant MAX_BPS = 10_000;

    IERC20 public immutable quote;
    address public owner;
    address public keeper;
    bool public paused;
    uint16 public minProfitBps;   // on-chain floor, independent of keeper input
    uint256 public maxNotional;   // per-tx exposure cap
    mapping(address => bool) public allowedTarget;

    uint256 private _lock = 1;
    address private _activeLender; // set only for the duration of flashRebalance

    event Executed(address indexed keeper, uint256 notional, uint256 netProfit, bool flash);
    event TargetSet(address indexed target, bool allowed);
    event ParamsSet(uint16 minProfitBps, uint256 maxNotional);
    event KeeperSet(address indexed keeper);
    event Paused(bool paused);

    error NotOwner();
    error NotKeeper();
    error IsPaused();
    error Reentrancy();
    error Expired(uint256 deadline);
    error NotionalCap(uint256 notional, uint256 cap);
    error TargetNotAllowed(address target);
    error LegFailed(uint256 index, bytes reason);
    error NetProfitBelowMin(int256 actual, uint256 required);
    error UntrustedLender(address lender);
    error UntrustedInitiator(address initiator);
    error BadParams();

    modifier onlyOwner() { if (msg.sender != owner) revert NotOwner(); _; }
    modifier onlyKeeper() { if (msg.sender != keeper && msg.sender != owner) revert NotKeeper(); _; }
    modifier live(uint256 deadline) {
        if (paused) revert IsPaused();
        if (block.timestamp > deadline) revert Expired(deadline);
        _;
    }
    modifier nonReentrant() {
        if (_lock != 1) revert Reentrancy();
        _lock = 2; _; _lock = 1;
    }

    constructor(IERC20 quote_, address keeper_, uint16 minProfitBps_, uint256 maxNotional_) {
        if (minProfitBps_ > MAX_BPS || maxNotional_ == 0) revert BadParams();
        quote = quote_;
        owner = msg.sender;
        keeper = keeper_;
        minProfitBps = minProfitBps_;
        maxNotional = maxNotional_;
    }

    // ----------------------------------------------------------------- admin
    function setTarget(address t, bool ok) external onlyOwner {
        if (t == address(this) || t == address(quote)) revert BadParams(); // no self/token calls
        allowedTarget[t] = ok;
        emit TargetSet(t, ok);
    }
    function setParams(uint16 bps, uint256 cap) external onlyOwner {
        if (bps > MAX_BPS || cap == 0) revert BadParams();
        (minProfitBps, maxNotional) = (bps, cap);
        emit ParamsSet(bps, cap);
    }
    function setKeeper(address k) external onlyOwner { keeper = k; emit KeeperSet(k); }
    function setPaused(bool p) external onlyOwner { paused = p; emit Paused(p); }
    function sweep(IERC20 token, address to, uint256 amt) external onlyOwner nonReentrant {
        require(token.transfer(to, amt), "transfer");
    }

    // ------------------------------------------------------------ execution
    /// @notice Own-capital route: legs spend vault balance and must return it plus profit.
    function execute(Leg[] calldata legs, uint256 notional, uint256 minNetProfit, uint256 deadline)
        external onlyKeeper live(deadline) nonReentrant returns (uint256 profit)
    {
        uint256 required = _required(notional, minNetProfit);
        uint256 pre = quote.balanceOf(address(this));
        _runLegs(legs);
        profit = _guard(pre, required);
        emit Executed(msg.sender, notional, profit, false);
    }

    /// @notice Flash route: borrow `notional` quote, run legs inside the callback, repay
    ///         principal + fee, and still clear the profit floor on the residual.
    function flashRebalance(
        IERC3156FlashLender lender, Leg[] calldata legs, uint256 notional,
        uint256 minNetProfit, uint256 deadline
    ) external onlyKeeper live(deadline) nonReentrant returns (uint256 profit) {
        uint256 required = _required(notional, minNetProfit);
        uint256 pre = quote.balanceOf(address(this));
        _activeLender = address(lender);
        lender.flashLoan(address(this), address(quote), notional, abi.encode(legs));
        _activeLender = address(0);
        profit = _guard(pre, required); // measured after lender pulled principal + fee
        emit Executed(msg.sender, notional, profit, true);
    }

    /// @dev Flash-rebalance hook. Only callable by the lender armed in flashRebalance,
    ///      and only for loans this vault initiated (blocks griefing via foreign initiators).
    function onFlashLoan(address initiator, address token, uint256 amount, uint256 fee, bytes calldata data)
        external override returns (bytes32)
    {
        if (msg.sender != _activeLender || _activeLender == address(0)) revert UntrustedLender(msg.sender);
        if (initiator != address(this) || token != address(quote)) revert UntrustedInitiator(initiator);
        Leg[] memory legs = abi.decode(data, (Leg[]));
        for (uint256 i; i < legs.length; ++i) _call(i, legs[i].target, legs[i].approveToken, legs[i].approveAmount, legs[i].data);
        require(quote.approve(msg.sender, amount + fee), "approve");
        return CALLBACK_SUCCESS;
    }

    // ------------------------------------------------------------- internal
    function _required(uint256 notional, uint256 minNetProfit) internal view returns (uint256) {
        if (notional > maxNotional) revert NotionalCap(notional, maxNotional);
        uint256 floor_ = (notional * minProfitBps) / MAX_BPS;
        return minNetProfit > floor_ ? minNetProfit : floor_;
    }

    function _guard(uint256 pre, uint256 required) internal view returns (uint256) {
        uint256 post = quote.balanceOf(address(this));
        int256 delta = int256(post) - int256(pre);
        if (delta < 0 || uint256(delta) < required) revert NetProfitBelowMin(delta, required);
        return uint256(delta);
    }

    function _runLegs(Leg[] calldata legs) internal {
        for (uint256 i; i < legs.length; ++i) _call(i, legs[i].target, legs[i].approveToken, legs[i].approveAmount, legs[i].data);
    }

    function _call(uint256 i, address target, address approveToken, uint256 approveAmount, bytes memory data) internal {
        if (!allowedTarget[target]) revert TargetNotAllowed(target);
        if (approveToken != address(0)) require(IERC20(approveToken).approve(target, approveAmount), "approve");
        (bool ok, bytes memory ret) = target.call(data);
        if (!ok) revert LegFailed(i, ret);
        if (approveToken != address(0)) require(IERC20(approveToken).approve(target, 0), "reset");
    }
}
