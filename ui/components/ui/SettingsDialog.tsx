"use client";

import React from "react";
import { Drawer, DrawerContent, DrawerTitle } from "@/components/ui/drawer";
import { ModalShell } from "@/components/ui/ModalShell";
import { useMediaQuery } from "@/hooks/useMediaQuery";

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
  isMobile?: boolean;
  title?: string;
}

export default function SettingsDialog({
  open,
  onOpenChange,
  children,
  isMobile: propIsMobile,
  title = "Dialog",
}: SettingsDialogProps) {
  const mediaQueryIsMobile = useMediaQuery("(max-width: 640px)");
  const isMobile = propIsMobile ?? mediaQueryIsMobile;

  if (isMobile) {
    return (
      <Drawer open={open} onOpenChange={onOpenChange}>
        <DrawerContent className="p-4">
          <DrawerTitle className="sr-only">{title}</DrawerTitle>
          {children}
        </DrawerContent>
      </Drawer>
    );
  }

  return (
    <ModalShell
      open={open}
      onClose={() => onOpenChange(false)}
      overlayClassName="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
      contentClassName="bg-card border border-border rounded-lg p-6 w-full max-w-md"
      closeOnOverlayClick
      contentAriaLabel={title}
    >
      {children}
    </ModalShell>
  );
}
