'use client';

import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  XCircle,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import type {
  CertificationRow,
  CertificationStatus,
  ProviderCertification,
} from '@/lib/api/services/admin';
import type {
  CertificationProgress,
  ModelCertificationResult,
} from '@/lib/provider-certification';
import { getCertificationResultStatus } from '@/lib/provider-certification';
import { cn } from '@/lib/utils';

const STATUS_STYLES: Record<
  CertificationStatus,
  { label: string; icon: typeof CheckCircle2; className: string }
> = {
  ok: {
    label: 'OK',
    icon: CheckCircle2,
    className:
      'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  },
  warn: {
    label: 'Warn',
    icon: AlertTriangle,
    className:
      'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  },
  fail: {
    label: 'Fail',
    icon: XCircle,
    className: 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400',
  },
};

export interface CertificationResultSummary {
  ok: number;
  warn: number;
  fail: number;
  error: number;
}

export function summarizeCertificationResults(
  results: ModelCertificationResult[]
): CertificationResultSummary {
  const summary = { ok: 0, warn: 0, fail: 0, error: 0 };
  for (const result of results) {
    summary[getCertificationResultStatus(result)] += 1;
  }
  return summary;
}

function StatusBadge({ status }: { status: CertificationStatus }) {
  const style = STATUS_STYLES[status];
  const Icon = style.icon;
  return (
    <Badge variant='outline' className={cn('gap-1', style.className)}>
      <Icon className='h-3 w-3' />
      {style.label}
    </Badge>
  );
}

function ResultStatusBadge({
  status,
}: {
  status: CertificationStatus | 'error';
}) {
  if (status === 'error') {
    return (
      <Badge
        variant='outline'
        className='border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400'
      >
        Error
      </Badge>
    );
  }
  return <StatusBadge status={status} />;
}

function RowItem({ row }: { row: CertificationRow }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const hasEvidence = Object.keys(row.evidence).length > 0;
  return (
    <li className='rounded-md border p-3 text-sm'>
      <div className='flex items-start justify-between gap-3'>
        <div className='min-w-0'>
          <div className='font-medium'>{row.title}</div>
          <div className='text-muted-foreground mt-0.5 break-words'>
            {row.detail}
          </div>
          <div className='text-muted-foreground mt-1 font-mono text-xs'>
            {row.id}
          </div>
        </div>
        <StatusBadge status={row.status} />
      </div>
      {hasEvidence && (
        <div className='mt-2'>
          <button
            type='button'
            onClick={() => setShowEvidence((value) => !value)}
            className='text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs'
          >
            {showEvidence ? (
              <ChevronDown className='h-3 w-3' />
            ) : (
              <ChevronRight className='h-3 w-3' />
            )}
            Evidence
          </button>
          {showEvidence && (
            <pre className='bg-muted mt-2 max-h-64 overflow-auto rounded p-2 font-mono text-xs'>
              {JSON.stringify(row.evidence, null, 2)}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

function CertificationReport({ report }: { report: ProviderCertification }) {
  const failing = report.rows.filter((row) => row.status === 'fail').length;
  const warning = report.rows.filter((row) => row.status === 'warn').length;

  return (
    <div className='space-y-4'>
      <ul className='grid gap-2 sm:grid-cols-2'>
        {report.checklist.map((goal) => {
          const style = STATUS_STYLES[goal.status];
          const Icon = style.icon;
          return (
            <li
              key={goal.goal}
              className={cn(
                'flex items-start gap-2 rounded-md border p-2 text-sm',
                style.className
              )}
            >
              <Icon className='mt-0.5 h-4 w-4 shrink-0' />
              <span>{goal.label}</span>
            </li>
          );
        })}
      </ul>
      <div className='text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs'>
        <span>
          {report.rows.length} checks · {failing} failed · {warning} warnings
        </span>
        <span>Generated {new Date(report.generated_at).toLocaleString()}</span>
      </div>
      <ul className='space-y-2'>
        {report.rows.map((row) => (
          <RowItem key={row.id} row={row} />
        ))}
      </ul>
    </div>
  );
}

interface ProviderCertificationResultsProps {
  results: ModelCertificationResult[];
  progress?: CertificationProgress | null;
  namesById: Map<string, string>;
  emptyMessage?: string;
}

export function ProviderCertificationResults({
  results,
  progress,
  namesById,
  emptyMessage = 'Results will appear here as certification completes.',
}: ProviderCertificationResultsProps) {
  const [activeResult, setActiveResult] = useState<string>('');

  useEffect(() => {
    if (
      results.length > 0 &&
      !results.some((result) => result.resultKey === activeResult)
    ) {
      setActiveResult(results[0].resultKey);
    }
  }, [activeResult, results]);

  return (
    <div className='flex min-h-0 flex-1 flex-col gap-2 overflow-hidden'>
      {progress && (
        <div className='text-muted-foreground flex shrink-0 items-center gap-2 rounded-md border p-3 text-sm'>
          <Loader2 className='h-4 w-4 animate-spin' />
          <span className='min-w-0 truncate'>
            Probing {namesById.get(progress.modelId) ?? progress.modelId}
            {progress.pathCount > 1
              ? ` across ${progress.pathCount} paths in parallel`
              : ''}{' '}
            — model {progress.modelIndex} of {progress.modelTotal}
          </span>
        </div>
      )}

      {results.length === 0 ? (
        <div className='text-muted-foreground flex min-h-40 flex-1 items-center justify-center text-center text-sm'>
          {emptyMessage}
        </div>
      ) : (
        <Tabs
          value={activeResult}
          onValueChange={setActiveResult}
          className='min-h-0 flex-1 overflow-hidden'
        >
          <div className='max-h-28 shrink-0 overflow-y-auto rounded-md border p-2'>
            <TabsList className='flex h-auto w-full flex-wrap justify-start gap-1 bg-transparent p-0'>
              {results.map((result, index) => {
                const status = getCertificationResultStatus(result);
                const Icon =
                  status === 'error' ? XCircle : STATUS_STYLES[status].icon;
                const modelRouteNumber = results
                  .slice(0, index + 1)
                  .filter((item) => item.modelId === result.modelId).length;
                const modelRouteCount = results.filter(
                  (item) => item.modelId === result.modelId
                ).length;
                return (
                  <TabsTrigger
                    key={result.resultKey}
                    value={result.resultKey}
                    title={namesById.get(result.modelId) ?? result.modelId}
                    className='h-7 max-w-44 min-w-0 gap-1 px-2 text-xs'
                  >
                    <Icon
                      className={cn(
                        status === 'ok' && 'text-emerald-600',
                        status === 'warn' && 'text-amber-600',
                        (status === 'fail' || status === 'error') &&
                          'text-red-600'
                      )}
                    />
                    <span className='truncate'>
                      {namesById.get(result.modelId) ?? result.modelId}
                    </span>
                    {modelRouteCount > 1 && (
                      <span className='text-muted-foreground'>
                        {modelRouteNumber}
                      </span>
                    )}
                  </TabsTrigger>
                );
              })}
            </TabsList>
          </div>

          <div className='min-h-0 flex-1 overflow-y-auto pr-1'>
            {results.map((result) => {
              const status = getCertificationResultStatus(result);
              return (
                <TabsContent
                  key={result.resultKey}
                  value={result.resultKey}
                  className='space-y-3'
                >
                  <div className='bg-muted/30 space-y-2 rounded-md border p-3'>
                    <div className='flex flex-wrap items-center justify-between gap-2'>
                      <div className='font-medium'>
                        {namesById.get(result.modelId) ?? result.modelId}
                      </div>
                      <ResultStatusBadge status={status} />
                    </div>
                    <div className='grid gap-1 text-xs'>
                      <span className='text-muted-foreground'>Model path</span>
                      <div className='bg-background rounded px-2 py-1.5 font-medium break-words'>
                        {result.pathLabel}
                      </div>
                    </div>
                  </div>
                  {result.report ? (
                    <CertificationReport report={result.report} />
                  ) : (
                    <div className='rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-400'>
                      {result.error ?? 'Certification failed'}
                    </div>
                  )}
                </TabsContent>
              );
            })}
          </div>
        </Tabs>
      )}
    </div>
  );
}
