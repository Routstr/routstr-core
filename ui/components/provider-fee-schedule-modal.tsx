'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Plus, Trash2 } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import {
  AdminService,
  FeeTimeRange,
  UpstreamProvider,
} from '@/lib/api/services/admin';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ProviderFeeScheduleModalProps {
  providers: UpstreamProvider[];
  /** Pre-selected provider IDs (e.g. clicked from a card). Empty = all selected. */
  initialSelectedIds?: number[];
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

interface RangeRow extends FeeTimeRange {
  _id: number;
}

// ---------------------------------------------------------------------------
// Overlap helpers (mirrored from backend logic)
// ---------------------------------------------------------------------------

function _toMinutes(t: string): number {
  const [h, m] = t.split(':').map(Number);
  return h * 60 + m;
}

function _rangeIntervals(start: string, end: string): Array<[number, number]> {
  const s = _toMinutes(start);
  const e = _toMinutes(end);
  if (s < e) return [[s, e]];
  if (s > e)
    return [
      [s, 1440],
      [0, e],
    ];
  return [[0, 1440]];
}

function _intervalsOverlap(a: [number, number], b: [number, number]): boolean {
  return a[0] < b[1] && b[0] < a[1];
}

function findOverlappingIds(rows: RangeRow[]): Set<number> {
  const overlapping = new Set<number>();
  for (let i = 0; i < rows.length; i++) {
    for (let j = i + 1; j < rows.length; j++) {
      const a = rows[i];
      const b = rows[j];
      if (!a.start_time || !a.end_time || !b.start_time || !b.end_time)
        continue;
      for (const ia of _rangeIntervals(a.start_time, a.end_time)) {
        for (const ib of _rangeIntervals(b.start_time, b.end_time)) {
          if (_intervalsOverlap(ia, ib)) {
            overlapping.add(a._id);
            overlapping.add(b._id);
          }
        }
      }
    }
  }
  return overlapping;
}

function isValidTime(t: string): boolean {
  return /^([01]\d|2[0-3]):([0-5]\d)$/.test(t);
}

function utcTimeNow(): string {
  const now = new Date();
  return now.toUTCString().slice(17, 22);
}

// Browsers may return "HH:MM:SS" from time inputs — strip seconds.
function normalizeTime(v: string): string {
  return v.slice(0, 5);
}

let _nextId = 1;

function makeRow(partial: Partial<FeeTimeRange> = {}): RangeRow {
  return {
    _id: _nextId++,
    start_time: partial.start_time ?? '',
    end_time: partial.end_time ?? '',
    provider_fee: partial.provider_fee ?? 1.05,
  };
}

// ---------------------------------------------------------------------------
// Sub-component: read-only range list under a provider
// ---------------------------------------------------------------------------

function ProviderRangePreview({ schedules }: { schedules: FeeTimeRange[] }) {
  if (schedules.length === 0) {
    return (
      <p className='text-muted-foreground pl-7 text-xs'>
        No scheduled ranges — default fee always applies.
      </p>
    );
  }
  return (
    <ul className='space-y-0.5 pl-7'>
      {schedules.map((s, i) => (
        <li key={i} className='flex items-center gap-2 text-xs'>
          <span className='text-muted-foreground font-mono'>
            {s.start_time} → {s.end_time} UTC
          </span>
          <Badge variant='outline' className='py-0 font-mono text-xs'>
            ×{s.provider_fee.toFixed(3)}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------

export function ProviderFeeScheduleModal({
  providers,
  initialSelectedIds,
  isOpen,
  onClose,
  onSuccess,
}: ProviderFeeScheduleModalProps) {
  const queryClient = useQueryClient();

  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [rows, setRows] = useState<RangeRow[]>([]);
  const [enforceOverride, setEnforceOverride] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);

  // Reset state when modal opens. If a single provider is pre-selected,
  // pre-populate the editor with its existing schedule so the user can edit it.
  useEffect(() => {
    if (!isOpen) return;

    const ids =
      initialSelectedIds && initialSelectedIds.length > 0
        ? new Set(initialSelectedIds)
        : new Set(providers.map((p) => p.id));

    setSelectedIds(ids);
    setEnforceOverride(false);

    if (initialSelectedIds && initialSelectedIds.length === 1) {
      const provider = providers.find((p) => p.id === initialSelectedIds[0]);
      const existing = provider?.provider_fee_schedules ?? [];
      setRows(existing.length > 0 ? existing.map((s) => makeRow(s)) : []);
      setEnforceOverride(true);
    } else {
      setRows([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  const overlapping = findOverlappingIds(rows);
  const allSelected =
    providers.length > 0 && selectedIds.size === providers.length;
  const noneSelected = selectedIds.size === 0;

  const toggleProvider = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelectedIds(
      allSelected ? new Set() : new Set(providers.map((p) => p.id))
    );
  };

  const addRow = () => setRows((prev) => [...prev, makeRow()]);

  const removeRow = (id: number) =>
    setRows((prev) => prev.filter((r) => r._id !== id));

  const updateRow = (
    id: number,
    field: keyof FeeTimeRange,
    value: string | number
  ) =>
    setRows((prev) =>
      prev.map((r) => (r._id === id ? { ...r, [field]: value } : r))
    );

  const hasValidationErrors =
    noneSelected ||
    rows.some(
      (r) =>
        !isValidTime(r.start_time) ||
        !isValidTime(r.end_time) ||
        r.provider_fee <= 0
    ) ||
    overlapping.size > 0;

  const handleSave = async () => {
    if (hasValidationErrors) return;
    const newSchedules: FeeTimeRange[] = rows.map(
      ({ start_time, end_time, provider_fee }) => ({
        start_time,
        end_time,
        provider_fee,
      })
    );
    setSaving(true);
    try {
      await Promise.all(
        [...selectedIds].map((id) => {
          const provider = providers.find((p) => p.id === id);
          const existing = provider?.provider_fee_schedules ?? [];

          let finalSchedules: FeeTimeRange[];
          if (enforceOverride) {
            finalSchedules = newSchedules;
          } else {
            // Only override ranges that overlap with ANY of the new ranges.
            // Keep existing non-overlapping ranges.
            const keptExisting = existing.filter((ex) => {
              return !newSchedules.some((nw) =>
                _rangeIntervals(ex.start_time, ex.end_time).some((ia) =>
                  _rangeIntervals(nw.start_time, nw.end_time).some((ib) =>
                    _intervalsOverlap(ia, ib)
                  )
                )
              );
            });
            finalSchedules = [...keptExisting, ...newSchedules];
          }

          return AdminService.updateFeeSchedules(id, finalSchedules);
        })
      );
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      toast.success(
        `Fee schedules saved for ${selectedIds.size} provider${selectedIds.size > 1 ? 's' : ''}`
      );
      onSuccess();
      onClose();
    } catch (err) {
      toast.error(
        `Failed to save: ${err instanceof Error ? err.message : 'Unknown error'}`
      );
    } finally {
      setSaving(false);
    }
  };

  const handleClearAll = async () => {
    if (noneSelected) return;
    setClearing(true);
    try {
      await Promise.all(
        [...selectedIds].map((id) => AdminService.deleteFeeSchedules(id))
      );
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      setRows([]);
      toast.success(
        `Fee schedules cleared for ${selectedIds.size} provider${selectedIds.size > 1 ? 's' : ''}`
      );
      onSuccess();
    } catch (err) {
      toast.error(
        `Failed to clear: ${err instanceof Error ? err.message : 'Unknown error'}`
      );
    } finally {
      setClearing(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[660px]'>
        <DialogHeader>
          <DialogTitle>Fee Schedules</DialogTitle>
          <DialogDescription>
            Select providers and configure time-based fee ranges (UTC). Outside
            scheduled ranges each provider&apos;s default fee applies. Current
            UTC time:{' '}
            <Badge variant='outline' className='font-mono'>
              {utcTimeNow()}
            </Badge>
          </DialogDescription>
        </DialogHeader>

        {/* Provider selection with existing-range read view */}
        <div className='space-y-2'>
          <div className='flex items-center justify-between'>
            <Label className='text-sm font-medium'>Apply to providers</Label>
            <button
              onClick={toggleAll}
              className='text-muted-foreground hover:text-foreground text-xs underline-offset-2 hover:underline'
            >
              {allSelected ? 'Deselect all' : 'Select all'}
            </button>
          </div>
          <div className='divide-y rounded-md border'>
            {providers.map((p) => (
              <div key={p.id} className='space-y-1.5 px-3 py-2'>
                <label className='hover:bg-muted/50 flex cursor-pointer items-center gap-3 rounded'>
                  <Checkbox
                    checked={selectedIds.has(p.id)}
                    onCheckedChange={() => toggleProvider(p.id)}
                  />
                  <span className='flex-1 text-sm font-medium'>
                    {p.provider_type}
                  </span>
                  <span className='text-muted-foreground truncate text-xs'>
                    {p.base_url}
                  </span>
                </label>
                <ProviderRangePreview
                  schedules={p.provider_fee_schedules ?? []}
                />
              </div>
            ))}
          </div>
          {noneSelected && (
            <p className='text-destructive text-xs'>
              Select at least one provider.
            </p>
          )}
        </div>

        {/* Fee range editor */}
        <div className='space-y-4'>
          <div className='flex items-center justify-between'>
            <Label className='text-sm font-medium'>
              New schedule{' '}
              {!enforceOverride && (
                <span className='text-muted-foreground font-normal'>
                  (merges with existing ranges, overriding only overlaps)
                </span>
              )}
            </Label>
            <div className='flex items-center space-x-2'>
              <Checkbox
                id='enforce-override'
                checked={enforceOverride}
                onCheckedChange={(checked) => setEnforceOverride(!!checked)}
              />
              <label
                htmlFor='enforce-override'
                className='text-xs leading-none font-medium peer-disabled:cursor-not-allowed peer-disabled:opacity-70'
              >
                Enforce overriding everything
              </label>
            </div>
          </div>

          <div className='space-y-2'>
            {rows.length === 0 && (
              <p className='text-muted-foreground rounded-md border border-dashed p-4 text-center text-sm'>
                No ranges configured — saving with no ranges will clear
                schedules.
              </p>
            )}

            {rows.map((row) => {
              const isOverlap = overlapping.has(row._id);
              const badTime =
                (row.start_time && !isValidTime(row.start_time)) ||
                (row.end_time && !isValidTime(row.end_time));
              const badFee = row.provider_fee <= 1.0;
              const hasError = isOverlap || badTime || badFee;

              return (
                <div
                  key={row._id}
                  className={`flex flex-col gap-2 rounded-md border p-3 sm:flex-row sm:items-end ${
                    hasError ? 'border-destructive bg-destructive/5' : ''
                  }`}
                >
                  <div className='flex flex-1 flex-col gap-1'>
                    <Label className='text-xs'>Start (UTC)</Label>
                    <input
                      type='time'
                      value={row.start_time}
                      onChange={(e) =>
                        updateRow(
                          row._id,
                          'start_time',
                          normalizeTime(e.target.value)
                        )
                      }
                      className='border-input bg-background ring-offset-background focus-visible:ring-ring flex h-10 w-full rounded-md border px-3 py-2 font-mono text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50'
                    />
                  </div>
                  <div className='flex flex-1 flex-col gap-1'>
                    <Label className='text-xs'>End (UTC)</Label>
                    <input
                      type='time'
                      value={row.end_time}
                      onChange={(e) =>
                        updateRow(
                          row._id,
                          'end_time',
                          normalizeTime(e.target.value)
                        )
                      }
                      className='border-input bg-background ring-offset-background focus-visible:ring-ring flex h-10 w-full rounded-md border px-3 py-2 font-mono text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50'
                    />
                  </div>
                  <div className='flex flex-1 flex-col gap-1'>
                    <Label className='text-xs'>Fee multiplier</Label>
                    <Input
                      type='number'
                      step='0.001'
                      min='0.001'
                      placeholder='1.05'
                      value={row.provider_fee}
                      onChange={(e) =>
                        updateRow(
                          row._id,
                          'provider_fee',
                          parseFloat(e.target.value) || 0
                        )
                      }
                    />
                  </div>
                  <Button
                    variant='ghost'
                    size='icon'
                    className='text-destructive hover:text-destructive shrink-0'
                    onClick={() => removeRow(row._id)}
                  >
                    <Trash2 className='h-4 w-4' />
                  </Button>
                </div>
              );
            })}
          </div>

          {overlapping.size > 0 && (
            <p className='text-destructive text-xs'>
              Some ranges overlap — fix them before saving.
            </p>
          )}
        </div>

        <div>
          <Button variant='outline' size='sm' onClick={addRow}>
            <Plus className='mr-1.5 h-4 w-4' />
            Add Range
          </Button>
        </div>

        <DialogFooter className='gap-2'>
          <Button
            variant='ghost'
            onClick={handleClearAll}
            disabled={clearing || noneSelected}
            className='text-destructive hover:text-destructive mr-auto'
          >
            {clearing ? 'Clearing…' : 'Clear Selected'}
          </Button>
          <Button variant='outline' onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving || hasValidationErrors}>
            {saving
              ? 'Saving…'
              : `Save to ${selectedIds.size} provider${selectedIds.size !== 1 ? 's' : ''}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
