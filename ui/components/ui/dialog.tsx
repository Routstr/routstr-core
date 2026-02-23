"use client";

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Drawer as DrawerPrimitive } from "vaul";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useMediaQuery } from "@/hooks/useMediaQuery";

const MOBILE_DIALOG_QUERY = "(max-width: 767px)";
const DialogMobileContext = React.createContext(false);

function useDialogMobileMode(): boolean {
  return React.useContext(DialogMobileContext);
}

function Dialog({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  const isMobile = useMediaQuery(MOBILE_DIALOG_QUERY);
  const { children, ...rootProps } = props;

  return (
    <DialogMobileContext.Provider value={isMobile}>
      {isMobile ? (
        <DrawerPrimitive.Root
          data-slot="dialog"
          {...(rootProps as React.ComponentProps<typeof DrawerPrimitive.Root>)}
        >
          {children}
        </DrawerPrimitive.Root>
      ) : (
        <DialogPrimitive.Root data-slot="dialog" {...rootProps}>
          {children}
        </DialogPrimitive.Root>
      )}
    </DialogMobileContext.Provider>
  );
}

function DialogTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  const isMobile = useDialogMobileMode();
  if (isMobile) {
    return (
      <DrawerPrimitive.Trigger
        data-slot="dialog-trigger"
        {...(props as React.ComponentProps<typeof DrawerPrimitive.Trigger>)}
      />
    );
  }
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />;
}

function DialogPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  const isMobile = useDialogMobileMode();
  if (isMobile) {
    return (
      <DrawerPrimitive.Portal
        data-slot="dialog-portal"
        {...(props as React.ComponentProps<typeof DrawerPrimitive.Portal>)}
      />
    );
  }
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />;
}

function DialogClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  const isMobile = useDialogMobileMode();
  if (isMobile) {
    return (
      <DrawerPrimitive.Close
        data-slot="dialog-close"
        {...(props as React.ComponentProps<typeof DrawerPrimitive.Close>)}
      />
    );
  }
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />;
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  const isMobile = useDialogMobileMode();
  if (isMobile) {
    return (
      <DrawerPrimitive.Overlay
        data-slot="dialog-overlay"
        className={cn(
          "fixed inset-0 z-50 bg-black/70 data-closed:animate-out data-open:animate-in data-closed:fade-out-0 data-open:fade-in-0",
          className
        )}
        {...(props as React.ComponentProps<typeof DrawerPrimitive.Overlay>)}
      />
    );
  }

  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-black/70 data-[state=closed]:animate-out data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
        className
      )}
      {...props}
    />
  );
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean;
}) {
  const isMobile = useDialogMobileMode();

  if (isMobile) {
    return (
      <DialogPortal data-slot="dialog-portal">
        <DialogOverlay />
        <DrawerPrimitive.Content
          data-slot="dialog-content"
          className={cn(
            "fixed inset-x-0 bottom-0 z-50 grid max-h-[90vh] w-full gap-4 rounded-t-xl border-t border-border bg-card p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] shadow-lg data-closed:animate-out data-open:animate-in data-closed:slide-out-to-bottom data-open:slide-in-from-bottom",
            className
          )}
          {...(props as React.ComponentProps<typeof DrawerPrimitive.Content>)}
        >
          <div
            className="bg-muted mx-auto -mt-1 mb-1 h-1 w-12 rounded-full"
            aria-hidden="true"
          />
          {children}
          {showCloseButton ? (
            <DrawerPrimitive.Close
              className="absolute top-3 right-3 rounded-xs opacity-70 transition-opacity hover:opacity-100 focus:outline-hidden focus:ring-1 focus:ring-ring disabled:pointer-events-none"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </DrawerPrimitive.Close>
          ) : null}
        </DrawerPrimitive.Content>
      </DialogPortal>
    );
  }

  return (
    <DialogPortal data-slot="dialog-portal">
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          "fixed top-[50%] left-[50%] z-50 grid w-full max-w-[calc(100%-2rem)] translate-x-[-50%] translate-y-[-50%] gap-4 border border-border bg-card p-6 shadow-lg duration-200 rounded-lg data-[state=closed]:animate-out data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
          className
        )}
        {...props}
      >
        {children}
        {showCloseButton ? (
          <DialogPrimitive.Close
            className="absolute top-4 right-4 rounded-xs opacity-70 transition-opacity hover:opacity-100 focus:outline-hidden focus:ring-1 focus:ring-ring disabled:pointer-events-none"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-1.5 text-center sm:text-left", className)}
      {...props}
    />
  );
}

function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn("flex flex-col-reverse gap-2 sm:flex-row sm:justify-end", className)}
      {...props}
    />
  );
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  const isMobile = useDialogMobileMode();
  if (isMobile) {
    return (
      <DrawerPrimitive.Title
        data-slot="dialog-title"
        className={cn("text-lg leading-none font-semibold", className)}
        {...(props as React.ComponentProps<typeof DrawerPrimitive.Title>)}
      />
    );
  }

  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn("text-lg leading-none font-semibold", className)}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  const isMobile = useDialogMobileMode();
  if (isMobile) {
    return (
      <DrawerPrimitive.Description
        data-slot="dialog-description"
        className={cn("text-sm text-muted-foreground", className)}
        {...(props as React.ComponentProps<typeof DrawerPrimitive.Description>)}
      />
    );
  }

  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogTrigger,
  DialogPortal,
  DialogClose,
  DialogOverlay,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
};
