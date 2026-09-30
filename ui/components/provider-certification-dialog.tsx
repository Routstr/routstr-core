'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2, RotateCcw } from 'lucide-react';

import { ProviderCertificationResults } from '@/components/provider-certification-results';
import {
  getCertificationModelNames,
  ProviderCertificationSetupPanel,
} from '@/components/provider-certification-setup';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useProviderCertificationRunner } from '@/hooks/use-provider-certification-runner';
import { AdminService } from '@/lib/api/services/admin';
import type { UpstreamProvider } from '@/lib/api/services/admin';
import {
  buildModelRuns,
  countCertificationTargets,
  emptyCertificationSetup,
  getModelsNeedingPath,
} from '@/lib/provider-certification';
import type { ProviderCertificationSetup } from '@/lib/provider-certification';

interface ProviderCertificationDialogProps {
  provider: UpstreamProvider;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ProviderCertificationDialog({
  provider,
  open,
  onOpenChange,
}: ProviderCertificationDialogProps) {
  const [workspaceTab, setWorkspaceTab] = useState<'setup' | 'results'>(
    'setup'
  );
  const [setup, setSetup] = useState<ProviderCertificationSetup>(
    emptyCertificationSetup
  );
  const { results, progress, isPending, run, reset } =
    useProviderCertificationRunner(provider.id);

  const models = useQuery({
    queryKey: ['provider-models', provider.id],
    queryFn: () => AdminService.getProviderModels(provider.id),
    enabled: open,
  });

  useEffect(() => {
    if (!open) {
      reset();
      setWorkspaceTab('setup');
      setSetup(emptyCertificationSetup());
    }
  }, [open, reset]);

  const modelRuns = buildModelRuns(setup, models.data);
  const targetCount = countCertificationTargets(modelRuns);
  const modelsNeedingPath = getModelsNeedingPath(setup);
  const namesById = getCertificationModelNames(models.data);

  const updateSetup = (nextSetup: ProviderCertificationSetup) => {
    setSetup(nextSetup);
    reset();
  };

  const runCertification = () => {
    setWorkspaceTab('results');
    void run(modelRuns, setup.checkCache);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) reset();
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent className='flex h-[90dvh] max-h-[90dvh] flex-col overflow-hidden sm:max-w-[780px]'>
        <DialogHeader className='shrink-0'>
          <DialogTitle>Certify upstream models</DialogTitle>
          <DialogDescription>
            Select models, then use the provider default, choose specific paths,
            or test every path. Models run one at a time; paths for the same
            model run in parallel against {provider.base_url}.
          </DialogDescription>
        </DialogHeader>

        <Tabs
          value={workspaceTab}
          onValueChange={(value) =>
            setWorkspaceTab(value as 'setup' | 'results')
          }
          className='min-h-0 flex-1 overflow-hidden'
        >
          <TabsList className='grid w-full shrink-0 grid-cols-2'>
            <TabsTrigger value='setup'>Setup</TabsTrigger>
            <TabsTrigger
              value='results'
              disabled={!isPending && results.length === 0}
            >
              Results
              {(isPending || results.length > 0) && (
                <Badge variant='secondary' className='ml-1 px-1.5 py-0 text-xs'>
                  {results.length}/{targetCount}
                </Badge>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value='setup'
            className='mt-0 min-h-0 overflow-hidden data-[state=active]:flex data-[state=active]:flex-col'
          >
            <div className='min-h-0 flex-1 overflow-y-auto pr-1'>
              <ProviderCertificationSetupPanel
                models={models.data}
                isLoading={models.isLoading}
                error={models.error}
                setup={setup}
                onChange={updateSetup}
                disabled={isPending}
                idPrefix={`certify-${provider.id}`}
              />
            </div>

            <div className='mt-3 flex shrink-0 justify-end border-t pt-3'>
              <Button
                variant='outline'
                size='sm'
                onClick={runCertification}
                disabled={
                  isPending ||
                  setup.selectedModelIds.length === 0 ||
                  modelsNeedingPath.length > 0
                }
                className='gap-1.5'
              >
                {isPending ? (
                  <Loader2 className='h-4 w-4 animate-spin' />
                ) : (
                  <RotateCcw className='h-4 w-4' />
                )}
                {isPending
                  ? progress
                    ? `Running ${progress.modelIndex} of ${progress.modelTotal}`
                    : 'Finishing in-flight probe'
                  : results.length > 0
                    ? `Run ${targetCount} route${targetCount === 1 ? '' : 's'} again`
                    : `Certify ${targetCount || ''} route${targetCount === 1 ? '' : 's'}`}
              </Button>
            </div>
          </TabsContent>

          <TabsContent
            value='results'
            className='mt-0 min-h-0 overflow-hidden data-[state=active]:flex data-[state=active]:flex-col'
          >
            <ProviderCertificationResults
              results={results}
              progress={progress}
              namesById={namesById}
            />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
