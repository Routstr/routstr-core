'use client';

import { useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { ConfigurationService } from '@/lib/api/services/configuration';

export function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    const publicPaths = ['/login', '/_register', '/unauthorized'];
    const isPublicPath = publicPaths.some((path) => pathname.startsWith(path));

    if (!isPublicPath && !ConfigurationService.isTokenValid()) {
      router.push('/login');
    }
  }, [router, pathname]);

  return <>{children}</>;
}
