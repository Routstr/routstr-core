import { z } from 'zod';

export const CreditTransactionTypeEnum = z.enum([
  'deposit',
  'refund',
  'usage',
  'withdrawal',
]);
export type CreditTransactionType = z.infer<typeof CreditTransactionTypeEnum>;

export const CreditTransactionSchema = z.object({
  id: z.string(),
  user_id: z.string(),
  api_key_id: z.string().optional(),
  amount: z.number().int(),
  transaction_type: CreditTransactionTypeEnum,
  provider_id: z.string().optional(),
  model_id: z.string().optional(),
  created_at: z.string().datetime(),
  status: z.enum(['pending', 'completed', 'failed', 'cancelled']),
  description: z.string().optional(),
});
export type CreditTransaction = z.infer<typeof CreditTransactionSchema>;

export const CreditBalanceSchema = z.object({
  user_id: z.string(),
  total_balance: z.number().int(),
  available_balance: z.number().int(),
  pending_balance: z.number().int(),
  last_updated: z.string().datetime(),
});
export type CreditBalance = z.infer<typeof CreditBalanceSchema>;

export const CreditUsageByApiKeySchema = z.object({
  api_key_id: z.string(),
  api_key_name: z.string(),
  total_spent: z.number().int(),
  usage_by_provider: z.record(z.string(), z.number().int()).optional(),
  usage_by_model: z.record(z.string(), z.number().int()).optional(),
});
export type CreditUsageByApiKey = z.infer<typeof CreditUsageByApiKeySchema>;

export const CreditUsageByPeriodSchema = z.object({
  period: z.string(),
  total_spent: z.number().int(),
  by_api_key: z.record(z.string(), z.number().int()).optional(),
  by_provider: z.record(z.string(), z.number().int()).optional(),
  by_model: z.record(z.string(), z.number().int()).optional(),
});
export type CreditUsageByPeriod = z.infer<typeof CreditUsageByPeriodSchema>;

export const CreditUsageStatisticsSchema = z.object({
  user_id: z.string(),
  total_spent: z.number().int(),
  by_api_key: z.array(CreditUsageByApiKeySchema),
  by_period: z.array(CreditUsageByPeriodSchema),
  from_date: z.string().datetime(),
  to_date: z.string().datetime(),
});
export type CreditUsageStatistics = z.infer<typeof CreditUsageStatisticsSchema>;

export const AddCreditsRequestSchema = z.object({
  amount: z.number().int().positive(),
  provider_id: z.string().optional(),
});
export type AddCreditsRequest = z.infer<typeof AddCreditsRequestSchema>;

export const RefundCreditsRequestSchema = z.object({
  amount: z.number().int().positive(),
  api_key_id: z.string().optional(),
});
export type RefundCreditsRequest = z.infer<typeof RefundCreditsRequestSchema>;

export const GetUsageStatisticsRequestSchema = z.object({
  from_date: z.string().datetime().optional(),
  to_date: z.string().datetime().optional(),
  group_by: z.enum(['day', 'week', 'month']).optional(),
  api_key_id: z.string().optional(),
});
export type GetUsageStatisticsRequest = z.infer<
  typeof GetUsageStatisticsRequestSchema
>;
