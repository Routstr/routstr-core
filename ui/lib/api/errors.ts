import { isAxiosError } from 'axios';

/**
 * Human-readable message for a failed API call. Prefers the backend's
 * `detail` field; never surfaces raw axios strings like
 * "Request failed with status code 500".
 */
export function getApiErrorMessage(
  error: unknown,
  fallback = 'Something went wrong'
): string {
  if (isAxiosError(error)) {
    if (!error.response) {
      return 'Cannot reach the node. Is it running?';
    }
    const detail: unknown = error.response.data?.detail;
    if (typeof detail === 'string' && detail) {
      return detail;
    }
    return `${fallback} (HTTP ${error.response.status})`;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}
