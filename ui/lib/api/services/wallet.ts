import { z } from 'zod';
import { apiClient } from '../client';

// Schema for redeeming tokens
export const RedeemTokenRequestSchema = z.object({
  token: z.string().min(1),
});

export const RedeemTokenResponseSchema = z.object({
  success: z.boolean(),
  amount: z.number().optional(),
  message: z.string().optional(),
});

// Schema for sending tokens
export const SendTokenRequestSchema = z.object({
  amount: z.number().positive(),
  offline: z.boolean().optional(),
});

export const SendTokenResponseSchema = z.object({
  token: z.string(),
  balance: z.number(),
  npub: z.string().optional(),
});

export type RedeemTokenRequest = z.infer<typeof RedeemTokenRequestSchema>;
export type RedeemTokenResponse = z.infer<typeof RedeemTokenResponseSchema>;
export type SendTokenRequest = z.infer<typeof SendTokenRequestSchema>;
export type SendTokenResponse = z.infer<typeof SendTokenResponseSchema>;

export class WalletService {
  static async redeemToken(token: string): Promise<RedeemTokenResponse> {
    try {
      const response = await apiClient.post<RedeemTokenResponse>(
        '/api/wallet/redeem',
        { token }
      );

      return response;
    } catch (error) {
      console.error('Error redeeming token:', error);
      return {
        success: false,
        message: 'Failed to redeem token. Please try again.',
      };
    }
  }

  static async sendToken(amount: number): Promise<SendTokenResponse> {
    try {
      const response = await apiClient.post<SendTokenResponse>(
        '/api/wallet/send',
        { amount }
      );

      return response;
    } catch (error) {
      console.error('Error generating token:', error);
      throw new Error('Failed to generate token. Please try again.');
    }
  }

  static async getBalance(): Promise<{ balance: number }> {
    try {
      const response = await apiClient.get<{ balance: number }>(
        '/api/wallet/balance'
      );

      return response;
    } catch (error) {
      console.error('Error fetching balance:', error);
      throw error;
    }
  }

  static async getDetailedBalances(): Promise<BalanceDetail[]> {
    try {
      const response = await apiClient.get<BalanceDetail[]>(
        '/admin/api/balances'
      );

      return response;
    } catch (error) {
      console.error('Error fetching detailed balances:', error);
      throw error;
    }
  }

  static async withdraw(
    amount: number,
    mintUrl?: string,
    unit: string = 'sat'
  ): Promise<WithdrawResponse> {
    try {
      const response = await apiClient.post<WithdrawResponse>(
        '/admin/withdraw',
        {
          amount,
          mint_url: mintUrl,
          unit,
        }
      );

      return response;
    } catch (error) {
      console.error('Error withdrawing funds:', error);
      throw error;
    }
  }
}

export interface BalanceDetail {
  mint_url: string;
  unit: string;
  wallet_balance: number;
  user_balance: number;
  owner_balance: number;
  error?: string;
}

export interface WithdrawResponse {
  token: string;
}
