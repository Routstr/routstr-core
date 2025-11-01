'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { Loader2, Copy, Check, SendIcon } from 'lucide-react';
import { toast } from 'sonner';
import { useQueryClient } from '@tanstack/react-query';
import { QRCodeSVG } from 'qrcode.react';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { WalletService } from '@/lib/api/services/wallet';

const formSchema = z.object({
  amount: z
    .string()
    .min(1, { message: 'Amount is required' })
    .refine((val) => !isNaN(Number(val)), {
      message: 'Amount must be a valid number',
    })
    .refine((val) => Number(val) > 0, {
      message: 'Amount must be greater than 0',
    }),
});

type FormValues = z.infer<typeof formSchema>;

export function EcashRedeem() {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [generatedToken, setGeneratedToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const queryClient = useQueryClient();

  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      amount: '',
    },
  });

  async function onSubmit(values: FormValues) {
    setIsSubmitting(true);
    try {
      // Use the wallet service to generate a token
      const result = await WalletService.sendToken(Number(values.amount));

      if (result.token) {
        setGeneratedToken(result.token);
        queryClient.invalidateQueries({ queryKey: ['wallet-balance'] });
      } else {
        toast.error('Failed to generate token. Please try again.');
      }
    } catch (error) {
      console.error('Error generating token:', error);
      toast.error(
        'An error occurred while generating the token. Please try again.'
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  const copyToClipboard = async () => {
    if (generatedToken) {
      try {
        await navigator.clipboard.writeText(generatedToken);
        setCopied(true);
        toast.success('Token copied to clipboard');
        setTimeout(() => setCopied(false), 2000);
      } catch {
        toast.error('Failed to copy token');
      }
    }
  };

  const handleReset = () => {
    setGeneratedToken(null);
    form.reset();
  };

  return (
    <Card className='h-full w-full shadow-sm'>
      <CardHeader>
        <div className='flex items-center space-x-2'>
          <SendIcon className='text-primary h-5 w-5' />
          <CardTitle>Send eCash</CardTitle>
        </div>
        <CardDescription>
          Generate a token to send eCash to someone
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!generatedToken ? (
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className='space-y-4'>
              <FormField
                control={form.control}
                name='amount'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Amount (sats)</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        placeholder='Enter amount to send'
                        {...field}
                      />
                    </FormControl>
                    <FormDescription>
                      Enter the amount of satoshis you want to send
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button
                type='submit'
                className='w-full'
                disabled={isSubmitting}
                size='lg'
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                    Generating...
                  </>
                ) : (
                  'Generate Token'
                )}
              </Button>
            </form>
          </Form>
        ) : (
          <div className='space-y-6'>
            <Tabs defaultValue='text' className='w-full'>
              <TabsList className='grid w-full grid-cols-2'>
                <TabsTrigger value='text'>Text</TabsTrigger>
                <TabsTrigger value='qr'>QR Code</TabsTrigger>
              </TabsList>
              <TabsContent value='text' className='py-4'>
                <div className='bg-muted/50 overflow-hidden rounded-md p-4'>
                  <div className='flex items-start justify-between gap-2'>
                    <pre className='font-mono text-sm break-all whitespace-pre-wrap'>
                      {generatedToken}
                    </pre>
                    <Button
                      variant='ghost'
                      size='icon'
                      onClick={copyToClipboard}
                      className='flex-shrink-0'
                    >
                      {copied ? (
                        <Check className='h-4 w-4' />
                      ) : (
                        <Copy className='h-4 w-4' />
                      )}
                    </Button>
                  </div>
                </div>
              </TabsContent>
              <TabsContent value='qr' className='flex justify-center py-4'>
                <div className='rounded-lg bg-white p-4'>
                  {generatedToken && (
                    <QRCodeSVG value={generatedToken} className='mx-auto' />
                  )}
                </div>
              </TabsContent>
            </Tabs>
            <Button variant='outline' onClick={handleReset} className='w-full'>
              Generate Another Token
            </Button>
          </div>
        )}
      </CardContent>
      <CardFooter className='text-muted-foreground flex justify-center border-t pt-4 text-sm'>
        {!generatedToken
          ? 'Tokens can only be redeemed once'
          : 'Share this token with the recipient to transfer funds'}
      </CardFooter>
    </Card>
  );
}
