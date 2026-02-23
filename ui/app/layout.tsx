import type { Metadata } from 'next';
import { GeistMono } from 'geist/font/mono';
import { GeistSans } from 'geist/font/sans';
import './globals.css';
import { Providers } from './providers';
import { SuppressHydrationWarning } from '@/components/suppress-hydration-warning';

export const metadata: Metadata = {
  title: 'Routstr',
  description: 'Routstr model management',
  icons: {
    icon: '/icon.ico',
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang='en' suppressHydrationWarning>
      <body
        className={`${GeistSans.variable} ${GeistMono.variable} font-sans antialiased`}
      >
        <SuppressHydrationWarning>
          <Providers>{children}</Providers>
        </SuppressHydrationWarning>
      </body>
    </html>
  );
}
