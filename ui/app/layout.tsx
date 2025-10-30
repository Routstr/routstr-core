import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';
import { Providers } from './providers';
import { SuppressHydrationWarning } from '@/components/suppress-hydration-warning';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
  preload: false,
  display: 'swap',
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
  preload: false,
  display: 'swap',
});

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
        className={`${geistSans.variable} ${geistMono.variable} antialiased font-sans`}
      >
        <SuppressHydrationWarning>
          <Providers>{children}</Providers>
        </SuppressHydrationWarning>
      </body>
    </html>
  );
}
