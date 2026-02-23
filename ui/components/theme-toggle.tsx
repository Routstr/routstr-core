'use client';

import { useEffect, useState } from 'react';
import { ChevronsUpDownIcon, Monitor, Moon, Sun } from 'lucide-react';
import { useTheme } from 'next-themes';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface ThemeToggleProps {
  className?: string;
  compact?: boolean;
  menuSide?: 'top' | 'right' | 'bottom' | 'left';
  menuAlign?: 'start' | 'center' | 'end';
}

type ThemeMode = 'light' | 'dark' | 'system';

const THEME_OPTIONS: Array<{ value: ThemeMode; label: string; icon: typeof Sun }> = [
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'system', label: 'System', icon: Monitor },
];

function isThemeMode(value: string | undefined): value is ThemeMode {
  return value === 'light' || value === 'dark' || value === 'system';
}

function getThemeOption(theme: ThemeMode) {
  return THEME_OPTIONS.find((option) => option.value === theme) ?? THEME_OPTIONS[2];
}

export function ThemeToggle({
  className,
  compact = false,
  menuSide = 'bottom',
  menuAlign = 'end',
}: ThemeToggleProps) {
  const { setTheme, theme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [fallbackTheme, setFallbackTheme] = useState<ThemeMode>('system');

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (isThemeMode(theme)) {
      setFallbackTheme(theme);
    }
  }, [theme]);

  const activeTheme = mounted && isThemeMode(theme) ? theme : fallbackTheme;
  const activeOption = getThemeOption(activeTheme);
  const ActiveIcon = activeOption.icon;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant='outline'
          size='sm'
          className={cn(
            'border-border/60 bg-background/65 text-muted-foreground hover:text-foreground rounded-md',
            compact ? 'h-8 w-10 justify-center px-0' : 'h-8 justify-between gap-2',
            className
          )}
          disabled={!mounted}
        >
          <span className='inline-flex min-w-0 items-center gap-1.5'>
            <ActiveIcon className='h-3.5 w-3.5 shrink-0' />
            {compact ? null : (
              <span className='truncate text-[11px] font-medium'>{activeOption.label}</span>
            )}
          </span>
          {compact ? (
            <span className='sr-only'>Theme: {activeOption.label}</span>
          ) : (
            <ChevronsUpDownIcon className='h-3.5 w-3.5 opacity-70' />
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side={menuSide} align={menuAlign}>
        <DropdownMenuRadioGroup
          value={activeTheme}
          onValueChange={(value) => {
            if (!isThemeMode(value)) return;
            setTheme(value);
          }}
        >
          {THEME_OPTIONS.map((option) => {
            const Icon = option.icon;

            return (
              <DropdownMenuRadioItem key={option.value} value={option.value}>
                <Icon className='h-3.5 w-3.5' />
                {option.label}
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
