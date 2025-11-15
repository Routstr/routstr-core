import { AdminService } from './api/services/admin';
import axios from 'axios';

export interface OnboardingStatus {
  needsOnboarding: boolean;
  hasAdminPassword: boolean;
  hasProviders: boolean;
  onboardingDismissed: boolean;
}

export async function checkOnboardingStatus(): Promise<OnboardingStatus> {
  const onboardingDismissed = 
    typeof window !== 'undefined' 
      ? localStorage.getItem('onboardingCompleted') === 'true'
      : false;

  if (onboardingDismissed) {
    return {
      needsOnboarding: false,
      hasAdminPassword: true,
      hasProviders: true,
      onboardingDismissed: true,
    };
  }

  let hasAdminPassword = true;
  let hasProviders = false;

  try {
    const providers = await AdminService.getUpstreamProviders();
    hasProviders = providers.length > 0;
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const statusCode = error.response?.status;
      const errorData = error.response?.data;
      
      if (statusCode === 500 && 
          (errorData?.detail === 'Admin password not configured' || 
           errorData?.message === 'Admin password not configured')) {
        hasAdminPassword = false;
      } else if (statusCode === 401 || statusCode === 403) {
        hasAdminPassword = false;
      }
    }
    hasProviders = false;
  }

  const needsOnboarding = !hasAdminPassword || !hasProviders;

  return {
    needsOnboarding,
    hasAdminPassword,
    hasProviders,
    onboardingDismissed: false,
  };
}

export function dismissOnboarding(): void {
  if (typeof window !== 'undefined') {
    localStorage.setItem('onboardingCompleted', 'true');
  }
}

export function resetOnboarding(): void {
  if (typeof window !== 'undefined') {
    localStorage.removeItem('onboardingCompleted');
  }
}
