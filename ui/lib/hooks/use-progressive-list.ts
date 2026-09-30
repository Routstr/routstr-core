'use client';

import { useEffect, useState } from 'react';

/**
 * Reveal a long list in frame-sized batches.
 *
 * A provider catalog can hold thousands of rows, and mounting them in one
 * commit blocks the main thread long enough that the page looks frozen right
 * after navigation. Each batch yields back to the browser, so the first rows
 * paint immediately and the rest fill in without freezing input.
 */
export function useProgressiveList<T>(
  items: T[],
  initialCount = 40,
  step = 80
): { visibleItems: T[]; hiddenCount: number } {
  const [count, setCount] = useState(initialCount);
  const [trackedItems, setTrackedItems] = useState(items);

  // Reset during render, not in an effect: an effect would first commit the new
  // list at the old (possibly full) count, which is the freeze this avoids.
  if (trackedItems !== items) {
    setTrackedItems(items);
    setCount(initialCount);
  }

  useEffect(() => {
    if (count >= items.length) {
      return;
    }

    const frame = requestAnimationFrame(() => {
      setCount((current) => Math.min(items.length, current + step));
    });

    return () => cancelAnimationFrame(frame);
  }, [count, items.length, step]);

  const visibleCount = Math.min(count, items.length);

  return {
    visibleItems: items.slice(0, visibleCount),
    hiddenCount: items.length - visibleCount,
  };
}
