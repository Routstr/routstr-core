'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  ExternalLinkIcon,
  InfoIcon,
  Loader2Icon,
  RefreshCwIcon,
} from 'lucide-react';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { Button } from '@/components/ui/button';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import {
  deriveStatus,
  formatReleaseDate,
  formatVersionLabel,
  parseVersion,
  type StatusKind,
} from '@/lib/utils/version';

interface NodeInfo {
  version?: string;
}

interface GithubRelease {
  tag_name: string;
  name?: string;
  html_url: string;
  published_at?: string;
  body?: string;
}

const NODE_QUERY_KEY = ['node-version'] as const;
const RELEASE_QUERY_KEY = ['routstr-latest-release'] as const;
const THIRTY_MINUTES = 30 * 60 * 1000;

const GITHUB_RELEASES_URL = `https://api.github.com/repos/Routstr/routstr-core/releases/latest`;
const RELEASES_PAGE_URL = `https://github.com/Routstr/routstr-core/releases`;

async function fetchNodeInfo(): Promise<NodeInfo> {
  const baseUrl = ConfigurationService.getLocalBaseUrl().replace(/\/+$/, '');
  const response = await fetch(`${baseUrl}/v1/info`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!response.ok) {
    throw new Error('Unable to load node info');
  }
  return (await response.json()) as NodeInfo;
}

async function fetchLatestRelease(): Promise<GithubRelease | null> {
  const response = await fetch(GITHUB_RELEASES_URL, {
    headers: { Accept: 'application/vnd.github+json' },
  });
  if (response.status === 403 || response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`GitHub responded ${response.status}`);
  }
  return (await response.json()) as GithubRelease;
}

function pickColorClass(status: StatusKind): string {
  if (status === 'outdated') return 'text-amber-600 dark:text-amber-400';
  if (status === 'unknown') return 'text-muted-foreground';
  if (status === 'ahead' || status === 'commit-drift') {
    return 'text-sky-600 dark:text-sky-400';
  }
  return 'text-emerald-600 dark:text-emerald-400';
}

function pickIcon(status: StatusKind) {
  if (status === 'outdated') return AlertTriangleIcon;
  if (status === 'commit-drift' || status === 'ahead' || status === 'unknown') {
    return InfoIcon;
  }
  return CheckCircle2Icon;
}

function describeStatus(status: StatusKind): string {
  if (status === 'outdated') return 'A newer release is available.';
  if (status === 'commit-drift') {
    return 'Running release version on a non-release commit.';
  }
  if (status === 'ahead') {
    return 'Running ahead of the latest published release.';
  }
  if (status === 'current') return 'Up to date with the latest release.';
  return 'Version status unavailable.';
}

interface VersionStatusProps {
  variant?: 'expanded' | 'compact';
  className?: string;
}

export function VersionStatus({
  variant = 'expanded',
  className,
}: VersionStatusProps) {
  const queryClient = useQueryClient();

  const nodeQuery = useQuery({
    queryKey: NODE_QUERY_KEY,
    queryFn: fetchNodeInfo,
    staleTime: THIRTY_MINUTES,
    retry: 1,
  });

  const releaseQuery = useQuery({
    queryKey: RELEASE_QUERY_KEY,
    queryFn: fetchLatestRelease,
    staleTime: THIRTY_MINUTES,
    refetchInterval: THIRTY_MINUTES,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const currentVersion = parseVersion(nodeQuery.data?.version);
  const latestVersion = parseVersion(releaseQuery.data?.tag_name);
  const status = deriveStatus(currentVersion, latestVersion);

  const isRefreshing = releaseQuery.isFetching || nodeQuery.isFetching;

  const handleRefresh = async (): Promise<void> => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: NODE_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: RELEASE_QUERY_KEY }),
    ]);
  };

  const colorClass = pickColorClass(status);
  const StatusIcon = pickIcon(status);
  const versionLabel = currentVersion
    ? formatVersionLabel(currentVersion)
    : nodeQuery.isLoading
      ? '…'
      : 'unknown';

  if (!nodeQuery.data && nodeQuery.isLoading && variant === 'expanded') {
    return null;
  }

  const statusDescription = describeStatus(status);
  const ariaLabel = `Node version ${versionLabel}. ${statusDescription} Click for details.`;

  const releaseRateLimited = releaseQuery.data === null;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type='button'
          onClick={(e) => e.stopPropagation()}
          className={cn(
            'hover:bg-accent/40 inline-flex items-center gap-1 rounded-md px-1 py-0.5 font-mono text-[10px] leading-tight transition-colors',
            colorClass,
            className
          )}
          title='View version details'
          aria-label={ariaLabel}
        >
          <StatusIcon className='h-3 w-3 shrink-0' />
          <span className='truncate'>{versionLabel}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        side='bottom'
        align='start'
        sideOffset={6}
        className='w-72 p-3'
      >
        <div className='flex items-start justify-between gap-2'>
          <div className='min-w-0 space-y-0.5'>
            <div className='flex items-center gap-1.5 text-sm font-medium'>
              <StatusIcon className={cn('h-4 w-4 shrink-0', colorClass)} />
              Node Version
            </div>
            <p className='text-muted-foreground text-xs leading-snug'>
              {statusDescription}
            </p>
          </div>
          <Button
            type='button'
            variant='outline'
            size='icon'
            className='h-7 w-7 shrink-0'
            onClick={handleRefresh}
            disabled={isRefreshing}
            title='Check for latest release'
          >
            {isRefreshing ? (
              <Loader2Icon className='h-3.5 w-3.5 animate-spin' />
            ) : (
              <RefreshCwIcon className='h-3.5 w-3.5' />
            )}
            <span className='sr-only'>Check for latest release</span>
          </Button>
        </div>

        <div className='mt-3 space-y-2'>
          <div className='border-border/60 bg-card/30 grid gap-1.5 rounded-md border p-2'>
            <div className='flex items-center justify-between gap-3'>
              <span className='text-muted-foreground text-[10px] tracking-wide uppercase'>
                Current
              </span>
              <span className='font-mono text-xs'>{versionLabel}</span>
            </div>
            {currentVersion?.commit ? (
              <div className='flex items-center justify-between gap-3'>
                <span className='text-muted-foreground text-[10px] tracking-wide uppercase'>
                  Commit
                </span>
                <code className='font-mono text-[11px]'>
                  {currentVersion.commit}
                </code>
              </div>
            ) : null}
          </div>

          <div className='border-border/60 bg-card/30 grid gap-1.5 rounded-md border p-2'>
            <div className='flex items-center justify-between gap-3'>
              <span className='text-muted-foreground text-[10px] tracking-wide uppercase'>
                Latest release
              </span>
              <span className='font-mono text-xs'>
                {releaseQuery.isLoading
                  ? 'loading…'
                  : releaseQuery.isError
                    ? 'unavailable'
                    : releaseRateLimited
                      ? 'rate-limited'
                      : (releaseQuery.data?.tag_name ?? 'unknown')}
              </span>
            </div>
            {releaseQuery.data?.published_at ? (
              <div className='flex items-center justify-between gap-3'>
                <span className='text-muted-foreground text-[10px] tracking-wide uppercase'>
                  Published
                </span>
                <span className='text-[11px]'>
                  {formatReleaseDate(releaseQuery.data.published_at)}
                </span>
              </div>
            ) : null}
          </div>

          {releaseQuery.isError ? (
            <p className='text-muted-foreground text-[11px]'>
              Failed to fetch latest release from GitHub.
            </p>
          ) : releaseRateLimited ? (
            <p className='text-muted-foreground text-[11px]'>
              GitHub rate limit reached. Try again later.
            </p>
          ) : null}
        </div>

        <div className='border-border/60 mt-3 border-t pt-2'>
          <a
            href={releaseQuery.data?.html_url ?? RELEASES_PAGE_URL}
            target='_blank'
            rel='noopener noreferrer'
            className='text-primary inline-flex items-center gap-1 text-xs hover:underline'
          >
            View release changelog
            <ExternalLinkIcon className='h-3 w-3' />
          </a>
        </div>
      </PopoverContent>
    </Popover>
  );
}
