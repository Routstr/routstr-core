# Withdrawal Token Persistence Fix

This document describes the fix for the admin panel withdrawal feature that was causing coin loss due to non-persistent cashu tokens.

## Problem

The original admin panel withdrawal feature had a critical issue:
- Generated cashu tokens were only displayed momentarily in the UI
- If the modal was closed accidentally, the token was lost forever
- No record of pending withdrawals was maintained
- This resulted in actual coin loss for administrators

## Solution

### 1. Token Persistence
- Added `PendingWithdrawal` database model to store all withdrawal tokens
- Every withdrawal is now automatically saved to the database
- Tokens are never lost, even if the UI is closed accidentally

### 2. Pending Withdrawals Display
- New "Pending Withdrawals" section in the admin panel
- Shows all unclaimed withdrawal tokens with details:
  - Amount and currency unit
  - Mint URL
  - Creation timestamp
  - Auto-send status
  - Lightning address (if auto-sent)
- Easy copy-to-clipboard functionality for tokens
- Ability to mark withdrawals as claimed when used

### 3. Automatic Lightning Address Sending
- Optional auto-send feature for withdrawals
- If `RECEIVE_LN_ADDRESS` is configured and auto-send is enabled:
  - Tokens are automatically sent to the Lightning address
  - No manual token handling required
  - Withdrawal is marked as completed automatically
- Fallback to manual token storage if auto-send fails

### 4. Enhanced UI/UX
- Checkbox option to enable auto-send during withdrawal
- Better feedback messages for successful operations
- Real-time updates of pending withdrawals list
- Improved error handling and user notifications

## Database Changes

### New Table: `pending_withdrawals`
```sql
CREATE TABLE pending_withdrawals (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL,
    amount INTEGER NOT NULL,
    unit TEXT NOT NULL,
    mint_url TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    claimed BOOLEAN NOT NULL DEFAULT FALSE,
    auto_sent BOOLEAN NOT NULL DEFAULT FALSE,
    ln_address TEXT,
    notes TEXT
);
```

## API Changes

### Enhanced `/admin/withdraw` endpoint
- New optional parameter: `auto_send_to_ln: bool`
- Returns additional fields:
  - `withdrawal_id`: Database ID of the stored withdrawal
  - `auto_sent`: Whether the token was automatically sent
  - `ln_address`: Lightning address used (if auto-sent)
  - `amount_received`: Amount received by Lightning address

### New endpoint: `/admin/api/mark-withdrawal-claimed/{withdrawal_id}`
- POST endpoint to mark a withdrawal as claimed
- Prevents accidental reuse of tokens
- Helps track withdrawal status

### New partial: `/admin/partials/pending-withdrawals`
- HTMX-powered component for displaying pending withdrawals
- Auto-refreshes when new withdrawals are created
- Provides interactive token management

## Configuration

### Environment Variables
The fix uses existing configuration:
- `RECEIVE_LN_ADDRESS`: Lightning address for auto-send functionality
- `ADMIN_PASSWORD`: Required for admin panel access

### Settings
Auto-send functionality respects the existing `receive_ln_address` setting that can be configured through the admin panel settings interface.

## Usage

### Manual Withdrawal (Default)
1. Click "💸 Withdraw Balance" in admin panel
2. Select mint and currency
3. Enter withdrawal amount
4. Click "Withdraw"
5. Token is generated and stored in pending withdrawals
6. Copy token from the result or from pending withdrawals list
7. Use token in your cashu wallet
8. Mark as claimed when used (optional)

### Auto-Send Withdrawal
1. Ensure `RECEIVE_LN_ADDRESS` is configured in settings
2. Click "💸 Withdraw Balance" in admin panel
3. Select mint and currency
4. Enter withdrawal amount
5. Check "Auto-send to Lightning address" checkbox
6. Click "Withdraw"
7. Funds are automatically sent to your Lightning address
8. Withdrawal is marked as completed automatically

## Migration

The database migration `123abc456def_add_pending_withdrawals_table.py` will be automatically applied when the application starts, creating the new `pending_withdrawals` table.

## Benefits

1. **No More Coin Loss**: All withdrawal tokens are permanently stored
2. **Better UX**: Clear visibility of all pending withdrawals
3. **Automation**: Optional auto-send reduces manual token handling
4. **Audit Trail**: Complete history of all withdrawals
5. **Recovery**: Ability to retrieve tokens even after UI accidents
6. **Flexibility**: Choice between manual tokens and automatic Lightning sends

## Backward Compatibility

This fix is fully backward compatible:
- Existing withdrawal functionality continues to work
- No breaking changes to existing APIs
- New features are opt-in
- Database migration is automatic and safe