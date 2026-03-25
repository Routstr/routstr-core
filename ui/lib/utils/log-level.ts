export type LogLevelBadgeVariant =
  | 'default'
  | 'secondary'
  | 'outline'
  | 'destructive';

export function getLogLevelBadgeVariant(level: string): LogLevelBadgeVariant {
  switch (level.toUpperCase()) {
    case 'INFO':
      return 'default';
    case 'WARNING':
      return 'secondary';
    case 'ERROR':
    case 'CRITICAL':
      return 'destructive';
    case 'TRACE':
    case 'DEBUG':
    default:
      return 'outline';
  }
}
