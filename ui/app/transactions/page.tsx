'use client';

import { useState } from 'react';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { useQuery } from '@tanstack/react-query';
import { Skeleton } from '@/components/ui/skeleton';
import {
  AlertCircle,
  Copy,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { toast } from 'sonner';
import { apiClient } from '@/lib/api/client';

interface Transaction {
  id: string;
  created_at: string;
  token: string;
  amount: string;
}

interface PaginatedTransactionsResponse {
  transactions: Transaction[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

const TransactionService = {
  getAllTransactions: async (): Promise<Transaction[]> => {
    try {
      const response = await apiClient.get<Transaction[]>('/api/transactions');
      return response || [];
    } catch (error) {
      console.error('Failed to fetch transactions:', error);
      throw new Error('Failed to fetch transactions');
    }
  },

  getPaginatedTransactions: async (
    page: number,
    perPage: number
  ): Promise<PaginatedTransactionsResponse> => {
    try {
      const response = await apiClient.get<PaginatedTransactionsResponse>(
        `/api/transactions/paginated/${page}/${perPage}`
      );
      return response;
    } catch (error) {
      console.error('Failed to fetch paginated transactions:', error);
      throw new Error('Failed to fetch paginated transactions');
    }
  },

  getRecentTransactions: async (limit: number): Promise<Transaction[]> => {
    try {
      const response = await apiClient.get<Transaction[]>(
        `/api/transactions/recent/${limit}`
      );
      return response || [];
    } catch (error) {
      console.error('Failed to fetch recent transactions:', error);
      throw new Error('Failed to fetch recent transactions');
    }
  },
};

export default function TransactionsPage() {
  const [currentPage, setCurrentPage] = useState(1);
  const perPage = 20;

  // Fetch paginated transactions data
  const {
    data: paginationData,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['transactions', currentPage, perPage],
    queryFn: () =>
      TransactionService.getPaginatedTransactions(currentPage, perPage),
    refetchOnWindowFocus: false,
    retry: 1,
    staleTime: 30000, // 30 seconds
  });

  const transactions = paginationData?.transactions || [];
  const totalPages = paginationData?.total_pages || 0;
  const total = paginationData?.total || 0;

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString();
  };

  const formatAmount = (amount: string) => {
    return `${parseInt(amount).toLocaleString()} msats`;
  };

  const truncateToken = (token: string) => {
    if (token.length <= 20) return token;
    return `${token.slice(0, 10)}...${token.slice(-10)}`;
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success('Token copied to clipboard!');
    } catch (error) {
      console.error('Failed to copy to clipboard:', error);
      toast.error('Failed to copy token');
    }
  };

  const goToPage = (page: number) => {
    if (page >= 1 && page <= totalPages) {
      setCurrentPage(page);
    }
  };

  const goToPrevious = () => {
    if (currentPage > 1) {
      setCurrentPage(currentPage - 1);
    }
  };

  const goToNext = () => {
    if (currentPage < totalPages) {
      setCurrentPage(currentPage + 1);
    }
  };

  return (
    <TooltipProvider>
      <SidebarProvider>
        <AppSidebar variant='inset' />
        <SidebarInset>
          <SiteHeader />
          <div className='flex flex-1 flex-col'>
            <div className='@container/main flex flex-1 flex-col gap-4 p-4 md:gap-8 md:p-8'>
              <div className='mb-6 flex items-center justify-between'>
                <div>
                  <h1 className='text-2xl font-bold tracking-tight'>
                    Transaction History
                  </h1>
                  <p className='text-muted-foreground text-sm'>
                    View all Cashu token transactions processed by the system
                  </p>
                </div>
                <Button
                  onClick={() => refetch()}
                  variant='outline'
                  size='sm'
                  disabled={isLoading}
                >
                  <RefreshCw
                    className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`}
                  />
                  Refresh
                </Button>
              </div>

              {isLoading ? (
                <div className='space-y-4'>
                  {[...Array(5)].map((_, i) => (
                    <Skeleton key={i} className='h-[120px] w-full' />
                  ))}
                </div>
              ) : error ? (
                <Alert variant='destructive'>
                  <AlertCircle className='h-4 w-4' />
                  <AlertDescription>
                    Failed to load transactions.{' '}
                    {error instanceof Error
                      ? error.message
                      : 'Please check if the server is running and try refreshing the page.'}
                  </AlertDescription>
                </Alert>
              ) : transactions.length === 0 ? (
                <div className='py-8 text-center'>
                  <p className='text-muted-foreground'>
                    No transactions found.
                  </p>
                </div>
              ) : (
                <div className='space-y-4'>
                  <div className='flex items-center justify-between'>
                    <div className='text-muted-foreground text-sm'>
                      Showing {(currentPage - 1) * perPage + 1} to{' '}
                      {Math.min(currentPage * perPage, total)} of {total}{' '}
                      transactions
                    </div>
                    <div className='text-muted-foreground text-sm'>
                      Page {currentPage} of {totalPages}
                    </div>
                  </div>

                  <div className='rounded-md border'>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className='w-[100px]'>ID</TableHead>
                          <TableHead>Date & Time</TableHead>
                          <TableHead>Amount</TableHead>
                          <TableHead className='w-[400px]'>
                            Cashu Token
                          </TableHead>
                          <TableHead className='w-[60px]'>Actions</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {transactions.map((transaction) => (
                          <TableRow key={transaction.id}>
                            <TableCell className='font-mono text-xs'>
                              {transaction.id.slice(0, 8)}
                            </TableCell>
                            <TableCell>
                              <div className='text-sm'>
                                {formatDate(transaction.created_at)}
                              </div>
                            </TableCell>
                            <TableCell>
                              <Badge variant='secondary'>
                                {formatAmount(transaction.amount)}
                              </Badge>
                            </TableCell>
                            <TableCell>
                              <div className='flex items-center gap-2'>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <p className='max-w-[300px] cursor-pointer truncate rounded px-1 py-0.5 font-mono text-xs hover:bg-gray-100'>
                                      {truncateToken(transaction.token)}
                                    </p>
                                  </TooltipTrigger>
                                  <TooltipContent className='max-w-md break-all'>
                                    <p className='font-mono text-xs'>
                                      {transaction.token}
                                    </p>
                                  </TooltipContent>
                                </Tooltip>
                              </div>
                            </TableCell>
                            <TableCell>
                              <Button
                                variant='ghost'
                                size='sm'
                                onClick={() =>
                                  copyToClipboard(transaction.token)
                                }
                                className='h-8 w-8 p-0'
                              >
                                <Copy className='h-4 w-4' />
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>

                  {/* Pagination Controls */}
                  {totalPages > 1 && (
                    <div className='flex items-center justify-between'>
                      <div className='text-muted-foreground text-sm'>
                        Page {currentPage} of {totalPages}
                      </div>
                      <div className='flex items-center space-x-2'>
                        <Button
                          variant='outline'
                          size='sm'
                          onClick={goToPrevious}
                          disabled={currentPage === 1}
                        >
                          <ChevronLeft className='mr-1 h-4 w-4' />
                          Previous
                        </Button>

                        {/* Page Numbers */}
                        <div className='flex items-center space-x-1'>
                          {Array.from(
                            { length: Math.min(5, totalPages) },
                            (_, i) => {
                              const pageNumber =
                                currentPage <= 3
                                  ? i + 1
                                  : currentPage >= totalPages - 2
                                    ? totalPages - 4 + i
                                    : currentPage - 2 + i;

                              if (pageNumber < 1 || pageNumber > totalPages)
                                return null;

                              return (
                                <Button
                                  key={pageNumber}
                                  variant={
                                    currentPage === pageNumber
                                      ? 'default'
                                      : 'outline'
                                  }
                                  size='sm'
                                  onClick={() => goToPage(pageNumber)}
                                  className='h-9 w-9 p-0'
                                >
                                  {pageNumber}
                                </Button>
                              );
                            }
                          )}
                        </div>

                        <Button
                          variant='outline'
                          size='sm'
                          onClick={goToNext}
                          disabled={currentPage === totalPages}
                        >
                          Next
                          <ChevronRight className='ml-1 h-4 w-4' />
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}
