# Onboarding Wizard

The Routstr onboarding wizard is an interactive setup guide that helps new users configure their AI routing system quickly and easily.

## Overview

The onboarding wizard automatically appears when:
- No admin password has been set, **OR**
- No upstream providers are configured

This ensures that essential setup steps are completed before the system is used.

## Features

### Multi-Step Setup Process

The wizard guides users through five key steps:

#### 1. Welcome Screen
- Introduces Routstr and its capabilities
- Explains what will be configured during onboarding
- Shows key features:
  - Admin access setup
  - AI provider connections
  - Node configuration

#### 2. Set Admin Password
- Secure the admin dashboard with a password
- Password must be at least 8 characters
- Confirms password to prevent typos
- Automatically logs in the user after setup

#### 3. Add AI Provider
- Connect the first AI provider to start routing requests
- Supports multiple provider types:
  - OpenRouter (default)
  - OpenAI
  - Anthropic
  - Azure
  - Groq
  - Perplexity
  - xAI
  - Fireworks
  - And more...
- Provides direct links to get API keys
- Validates API key presence before proceeding

#### 4. Optional Settings
- Configure node name and description
- Can be skipped if desired
- Settings can be changed later in the Settings page

#### 5. Completion
- Confirms successful setup
- Provides next steps:
  - Add more providers
  - Configure models
  - Monitor usage
- Redirects to dashboard

## User Experience

### Visual Design
- Progress bar shows completion percentage
- Step counter (e.g., "Step 2 of 5")
- Consistent iconography for each step
- Clear navigation with Back/Next buttons

### Skip Option
- "Skip for now" button on the welcome screen
- Allows advanced users to configure manually
- Marks onboarding as completed

### Persistence
- Uses localStorage to track completion
- Won't show again once completed
- Can be reset by clearing browser storage

## Technical Implementation

### Files Created/Modified

1. **`/ui/components/onboarding-wizard.tsx`**
   - Main onboarding component
   - Multi-step wizard logic
   - Form validation and submission
   - API integration

2. **`/ui/lib/onboarding.ts`**
   - Onboarding status checking
   - Persistence management
   - Determines if onboarding is needed

3. **`/ui/app/page.tsx`**
   - Integration point on dashboard
   - Status check on load
   - Shows wizard when needed

4. **`/ui/lib/api/client.ts`**
   - Added `setAuthToken` method
   - Handles authentication after setup

### API Endpoints Used

- `POST /admin/api/setup` - Initial password setup (no auth required)
- `POST /admin/api/login` - Login after setup
- `GET /admin/api/provider-types` - List available providers
- `POST /admin/api/upstream-providers` - Create provider
- `PATCH /admin/api/settings` - Update node settings

### State Management

The wizard uses React Query for:
- Fetching provider types
- Creating providers
- Updating settings
- Managing loading states

### Error Handling

- Validates password length and match
- Requires API key for provider setup
- Shows user-friendly error messages
- Handles API failures gracefully

## Security Considerations

1. **Password Requirements**
   - Minimum 8 characters
   - Must match confirmation
   - Stored securely in backend

2. **API Key Storage**
   - Keys are sent securely to backend
   - Never stored in localStorage
   - Transmitted over HTTPS in production

3. **Authentication Flow**
   - Setup endpoint doesn't require auth
   - Subsequent steps use JWT token
   - Token stored with expiration

## Customization

### Adding New Steps

To add a step to the wizard:

1. Add step definition to `steps` array:
```typescript
{
  id: 'mystep',
  title: 'My Step Title',
  description: 'Step description',
  icon: <MyIcon className="h-12 w-12 text-color-500" />,
}
```

2. Add case to `renderStepContent()` switch
3. Add validation logic to `canProceed()`
4. Add submission logic to `handleNext()`

### Modifying Conditions

To change when onboarding appears, edit `checkOnboardingStatus()` in `/ui/lib/onboarding.ts`.

### Resetting Onboarding

To force onboarding to appear again:
```javascript
localStorage.removeItem('onboardingCompleted');
```

Or use the helper:
```typescript
import { resetOnboarding } from '@/lib/onboarding';
resetOnboarding();
```

## Testing

### Manual Testing Checklist

- [ ] Onboarding appears on first visit
- [ ] Password validation works correctly
- [ ] Provider creation succeeds with valid API key
- [ ] Optional settings can be skipped
- [ ] Completion redirects to dashboard
- [ ] Skip button works on welcome screen
- [ ] Back button navigates correctly
- [ ] Progress bar updates accurately
- [ ] Error messages display properly
- [ ] Authentication persists after setup

### Testing Fresh Installation

1. Clear browser localStorage
2. Ensure no admin password is set in backend
3. Ensure no providers exist
4. Visit dashboard
5. Verify onboarding appears automatically

## Future Enhancements

Potential improvements:
- Add model configuration step
- Include wallet setup guidance
- Provide API testing step
- Add video tutorials
- Support multiple languages
- Enable custom provider configurations
