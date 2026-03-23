'use client';

import { useState, useEffect } from 'react';
import * as React from 'react';
import { Input } from '@/components/ui/input';

interface ApiKeyInputProps extends React.ComponentProps<'input'> {
  onApiKeyChange: (apiKey: string) => void;
}

export function ApiKeyInput({
  value,
  onApiKeyChange,
  ...props
}: ApiKeyInputProps) {
  const [internalValue, setInternalValue] = useState(value || '');

  useEffect(() => {
    setInternalValue(value || '');
  }, [value]);

  useEffect(() => {
    const handler = setTimeout(() => {
      onApiKeyChange(internalValue as string);
    }, 300);

    return () => clearTimeout(handler);
  }, [internalValue, onApiKeyChange]);

  return (
    <Input
      value={internalValue}
      onChange={(e) => setInternalValue(e.target.value)}
      placeholder='sk-...'
      className='font-mono text-sm'
      {...props}
    />
  );
}
