'use client';

import { useEffect, useState } from 'react';

interface SuppressHydrationWarningProps {
  children: React.ReactNode;
}

export function SuppressHydrationWarning({
  children,
}: SuppressHydrationWarningProps) {
  const [isClient, setIsClient] = useState(false);

  useEffect(() => {
    setIsClient(true);
  }, []);

  if (!isClient) {
    return null;
  }

  return <>{children}</>;
}
