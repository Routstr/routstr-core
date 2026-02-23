import type { Dispatch, SetStateAction } from 'react';
import type { CreateUpstreamProvider, ProviderType } from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import {
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ProviderFormFields } from '@/components/provider-form-fields';

interface ProviderFormDialogContentProps {
  mode: 'create' | 'edit';
  title: string;
  description: string;
  submitLabel: string;
  submittingLabel: string;
  formData: CreateUpstreamProvider;
  setFormData: Dispatch<SetStateAction<CreateUpstreamProvider>>;
  providerTypes: ProviderType[];
  providerFeePlaceholder: string;
  docsLinkClassName: string;
  canCreateAccount: boolean;
  isCreatingAccount: boolean;
  onCreateAccount: () => void;
  onCancel: () => void;
  onSubmit: () => void;
  isSubmitting: boolean;
}

export function ProviderFormDialogContent({
  mode,
  title,
  description,
  submitLabel,
  submittingLabel,
  formData,
  setFormData,
  providerTypes,
  providerFeePlaceholder,
  docsLinkClassName,
  canCreateAccount,
  isCreatingAccount,
  onCreateAccount,
  onCancel,
  onSubmit,
  isSubmitting,
}: ProviderFormDialogContentProps) {
  return (
    <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[500px]'>
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <ProviderFormFields
        mode={mode}
        formData={formData}
        setFormData={setFormData}
        providerTypes={providerTypes}
        providerFeePlaceholder={providerFeePlaceholder}
        docsLinkClassName={docsLinkClassName}
        canCreateAccount={canCreateAccount}
        isCreatingAccount={isCreatingAccount}
        onCreateAccount={onCreateAccount}
      />
      <DialogFooter>
        <Button variant='outline' onClick={onCancel} className='w-full sm:w-auto'>
          Cancel
        </Button>
        <Button onClick={onSubmit} disabled={isSubmitting} className='w-full sm:w-auto'>
          {isSubmitting ? submittingLabel : submitLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
