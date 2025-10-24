import { apiClient } from '../client';
import { z } from 'zod';

export const UpstreamProviderSchema = z.object({
  id: z.number(),
  provider_type: z.string(),
  base_url: z.string(),
  api_key: z.string().optional(),
  api_version: z.string().nullable().optional(),
  enabled: z.boolean(),
  provider_fee: z.number().optional(),
});

export const CreateUpstreamProviderSchema = z.object({
  provider_type: z.string(),
  base_url: z.string(),
  api_key: z.string(),
  api_version: z.string().nullable().optional(),
  enabled: z.boolean().default(true),
  provider_fee: z.number().optional(),
});

export const UpdateUpstreamProviderSchema = z.object({
  provider_type: z.string().optional(),
  base_url: z.string().optional(),
  api_key: z.string().optional(),
  api_version: z.string().nullable().optional(),
  enabled: z.boolean().optional(),
  provider_fee: z.number().optional(),
});

export const AdminModelPricingSchema = z.object({
  prompt: z.number().optional(),
  completion: z.number().optional(),
  request: z.number().optional(),
  image: z.number().optional(),
  web_search: z.number().optional(),
  internal_reasoning: z.number().optional(),
});

export const AdminModelArchitectureSchema = z.object({
  modality: z.string().optional(),
  input_modalities: z.array(z.string()).optional(),
  output_modalities: z.array(z.string()).optional(),
  tokenizer: z.string().optional(),
  instruct_type: z.string().nullable().optional(),
});

export const AdminModelSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  created: z.number(),
  context_length: z.number(),
  architecture: AdminModelArchitectureSchema.or(z.record(z.any())),
  pricing: AdminModelPricingSchema.or(z.record(z.any())),
  per_request_limits: z.record(z.any()).nullable().optional(),
  top_provider: z.record(z.any()).nullable().optional(),
  upstream_provider_id: z.number().nullable().optional(),
  enabled: z.boolean().default(true),
});

export const ProviderModelsSchema = z.object({
  provider: z.object({
    id: z.number(),
    provider_type: z.string(),
    base_url: z.string(),
  }),
  db_models: z.array(AdminModelSchema),
  remote_models: z.array(AdminModelSchema),
});

export type UpstreamProvider = z.infer<typeof UpstreamProviderSchema>;
export type CreateUpstreamProvider = z.infer<
  typeof CreateUpstreamProviderSchema
>;
export type UpdateUpstreamProvider = z.infer<
  typeof UpdateUpstreamProviderSchema
>;
export type AdminModel = z.infer<typeof AdminModelSchema>;
export type AdminModelPricing = z.infer<typeof AdminModelPricingSchema>;
export type AdminModelArchitecture = z.infer<
  typeof AdminModelArchitectureSchema
>;
export type ProviderModels = z.infer<typeof ProviderModelsSchema>;

export interface AdminModelAsModel {
  id: string;
  name: string;
  full_name: string;
  description?: string;
  modelType: string;
  isEnabled: boolean;
  createdAt: string;
  updatedAt: string;
  provider: string;
  url: string;
  api_key?: string;
  input_cost: number;
  output_cost: number;
  min_cost_per_request: number;
  min_cash_per_request: number;
  contextLength?: number;
  apiKeyRequired: boolean;
  provider_id?: string;
  is_free: boolean;
  soft_deleted?: boolean;
  has_own_api_key: boolean;
  api_key_type: string;
}

export interface AdminModelGroup {
  id: string;
  provider: string;
  group_api_key?: string;
  group_url?: string;
  created_at: string;
  updated_at: string;
}

export class AdminService {
  static convertPricingToPerMillionTokens(
    pricing: Record<string, unknown>
  ): Record<string, unknown> {
    if (!pricing) return pricing;
    const result = { ...pricing };
    if (typeof result.prompt === 'number') {
      result.prompt = result.prompt * 1000000;
    }
    if (typeof result.completion === 'number') {
      result.completion = result.completion * 1000000;
    }
    if (typeof result.request === 'number') {
      result.request = result.request * 1000000;
    }
    if (typeof result.image === 'number') {
      result.image = result.image * 1000000;
    }
    return result;
  }

  static convertPricingToPerToken(
    pricing: Record<string, unknown>
  ): Record<string, unknown> {
    if (!pricing) return pricing;
    const result = { ...pricing };
    if (typeof result.prompt === 'number') {
      result.prompt = result.prompt / 1000000;
    }
    if (typeof result.completion === 'number') {
      result.completion = result.completion / 1000000;
    }
    if (typeof result.request === 'number') {
      result.request = result.request / 1000000;
    }
    if (typeof result.image === 'number') {
      result.image = result.image / 1000000;
    }
    return result;
  }

  static transformAdminModelToModel(
    adminModel: AdminModel,
    providerName?: string
  ): AdminModelAsModel {
    const pricing = this.convertPricingToPerMillionTokens(adminModel.pricing);
    const inputCost = (pricing?.prompt as number) || 0;
    const outputCost = (pricing?.completion as number) || 0;
    const requestCost = (pricing?.request as number) || 0;

    return {
      id: adminModel.id,
      name: adminModel.name,
      full_name: adminModel.name,
      description: adminModel.description,
      modelType:
        ((adminModel.architecture as Record<string, unknown>)
          ?.modality as string) || 'text',
      isEnabled: adminModel.enabled,
      createdAt: new Date(adminModel.created * 1000).toISOString(),
      updatedAt: new Date(adminModel.created * 1000).toISOString(),
      provider: providerName || 'Unknown',
      url: '',
      api_key: undefined,
      input_cost: inputCost,
      output_cost: outputCost,
      min_cost_per_request: requestCost,
      min_cash_per_request: 0,
      contextLength: adminModel.context_length,
      apiKeyRequired: true,
      provider_id: adminModel.upstream_provider_id?.toString(),
      is_free: inputCost === 0 && outputCost === 0,
      soft_deleted: !adminModel.enabled,
      has_own_api_key: false,
      api_key_type: 'group',
    };
  }

  static transformModelToAdminModel(model: AdminModelAsModel): AdminModel {
    const pricing = this.convertPricingToPerToken({
      prompt: model.input_cost,
      completion: model.output_cost,
      request: model.min_cost_per_request,
      image: 0,
      web_search: 0,
      internal_reasoning: 0,
    });
    return {
      id: model.id,
      name: model.name,
      description: model.description || '',
      created: Math.floor(new Date(model.createdAt).getTime() / 1000),
      context_length: model.contextLength || 0,
      architecture: {
        modality: model.modelType,
        input_modalities: [model.modelType],
        output_modalities: [model.modelType],
        tokenizer: '',
        instruct_type: null,
      },
      pricing,
      per_request_limits: null,
      top_provider: null,
      upstream_provider_id: model.provider_id
        ? parseInt(model.provider_id)
        : null,
      enabled: model.isEnabled,
    };
  }

  static async getUpstreamProviders(): Promise<UpstreamProvider[]> {
    return await apiClient.get<UpstreamProvider[]>(
      '/admin/api/upstream-providers'
    );
  }

  static async getUpstreamProvider(id: number): Promise<UpstreamProvider> {
    return await apiClient.get<UpstreamProvider>(
      `/admin/api/upstream-providers/${id}`
    );
  }

  static async createUpstreamProvider(
    data: CreateUpstreamProvider
  ): Promise<UpstreamProvider> {
    return await apiClient.post<UpstreamProvider>(
      '/admin/api/upstream-providers',
      data
    );
  }

  static async updateUpstreamProvider(
    id: number,
    data: UpdateUpstreamProvider
  ): Promise<UpstreamProvider> {
    return await apiClient.patch<UpstreamProvider>(
      `/admin/api/upstream-providers/${id}`,
      data
    );
  }

  static async deleteUpstreamProvider(
    id: number
  ): Promise<{ ok: boolean; deleted_id: number }> {
    return await apiClient.delete<{ ok: boolean; deleted_id: number }>(
      `/admin/api/upstream-providers/${id}`
    );
  }

  static async getProviderModels(providerId: number): Promise<ProviderModels> {
    const data = await apiClient.get<ProviderModels>(
      `/admin/api/upstream-providers/${providerId}/models`
    );
    return data;
  }

  static async createProviderModel(
    providerId: number,
    data: AdminModel
  ): Promise<AdminModel> {
    const payload = {
      ...data,
      pricing: this.convertPricingToPerToken(data.pricing),
    };
    console.log('Creating provider model - pricing conversion:', {
      original: data.pricing,
      converted: payload.pricing,
    });
    const model = await apiClient.post<AdminModel>(
      `/admin/api/upstream-providers/${providerId}/models`,
      payload
    );
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async getProviderModel(
    providerId: number,
    modelId: string
  ): Promise<AdminModel> {
    const model = await apiClient.get<AdminModel>(
      `/admin/api/upstream-providers/${providerId}/models/${encodeURIComponent(modelId)}`
    );
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async getModel(
    modelId: string,
    providerId: number | null = null
  ): Promise<AdminModel> {
    const model = await apiClient.post<AdminModel>('/admin/api/models/get', {
      model_id: modelId,
      provider_id: providerId,
    });
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async updateProviderModel(
    providerId: number,
    modelId: string,
    data: AdminModel
  ): Promise<AdminModel> {
    const payload = {
      ...data,
      pricing: this.convertPricingToPerToken(data.pricing),
    };
    console.log('Updating provider model - pricing conversion:', {
      original: data.pricing,
      converted: payload.pricing,
    });
    const model = await apiClient.patch<AdminModel>(
      `/admin/api/upstream-providers/${providerId}/models/${encodeURIComponent(modelId)}`,
      payload
    );
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async deleteProviderModel(
    providerId: number,
    modelId: string
  ): Promise<{ ok: boolean; deleted_id: string }> {
    return await apiClient.delete<{ ok: boolean; deleted_id: string }>(
      `/admin/api/upstream-providers/${providerId}/models/${encodeURIComponent(modelId)}`
    );
  }

  static async deleteAllProviderModels(
    providerId: number
  ): Promise<{ ok: boolean; deleted: number }> {
    return await apiClient.delete<{ ok: boolean; deleted: number }>(
      `/admin/api/upstream-providers/${providerId}/models`
    );
  }

  static async getModelsWithProviders(): Promise<{
    models: AdminModelAsModel[];
    groups: AdminModelGroup[];
  }> {
    const providers = await this.getUpstreamProviders();

    const groups: AdminModelGroup[] = providers.map((p) => ({
      id: p.id.toString(),
      provider: p.provider_type,
      group_api_key: p.api_key,
      group_url: p.base_url,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }));

    const allModels: AdminModelAsModel[] = [];
    const seenModelIds = new Set<string>();

    for (const provider of providers) {
      try {
        const providerModels = await this.getProviderModels(provider.id);

        providerModels.db_models.forEach((dbModel) => {
          seenModelIds.add(dbModel.id);
          const modelWithProvider = {
            ...dbModel,
            upstream_provider_id: provider.id,
          };
          allModels.push({
            ...this.transformAdminModelToModel(
              modelWithProvider,
              provider.provider_type
            ),
            has_own_api_key: false,
            api_key_type: 'group',
          });
        });

        providerModels.remote_models.forEach((remoteModel) => {
          if (!seenModelIds.has(remoteModel.id)) {
            const modelWithProvider = {
              ...remoteModel,
              upstream_provider_id: provider.id,
            };
            allModels.push({
              ...this.transformAdminModelToModel(
                modelWithProvider,
                provider.provider_type
              ),
              has_own_api_key: false,
              api_key_type: 'remote',
              soft_deleted: false,
            });
          }
        });
      } catch (error) {
        console.error(
          `Failed to fetch models for provider ${provider.id}:`,
          error
        );
      }
    }

    return { models: allModels, groups };
  }

  static async createModelGroup(data: {
    provider: string;
    group_api_key?: string;
    group_url?: string;
  }): Promise<AdminModelGroup> {
    const provider = await this.createUpstreamProvider({
      provider_type: data.provider,
      base_url: data.group_url || '',
      api_key: data.group_api_key || '',
      enabled: true,
    });
    return {
      id: provider.id.toString(),
      provider: provider.provider_type,
      group_api_key: provider.api_key,
      group_url: provider.base_url,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
  }

  static async updateModelGroup(
    id: string,
    data: { provider?: string; group_api_key?: string; group_url?: string }
  ): Promise<AdminModelGroup> {
    const provider = await this.updateUpstreamProvider(parseInt(id), {
      provider_type: data.provider,
      base_url: data.group_url,
      api_key: data.group_api_key,
    });
    return {
      id: provider.id.toString(),
      provider: provider.provider_type,
      group_api_key: provider.api_key,
      group_url: provider.base_url,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
  }

  static async createModel(
    data: Record<string, unknown>
  ): Promise<AdminModelAsModel> {
    const providerId = data.provider_id
      ? parseInt(data.provider_id as string)
      : null;

    const pricing = {
      prompt: (data.input_cost as number) / 1000000,
      completion: (data.output_cost as number) / 1000000,
      request: (data.min_cost_per_request as number) || 0,
      image: 0,
      web_search: 0,
      internal_reasoning: 0,
    };

    const modelId = (data.id as string) || (data.full_name as string);

    const payload = {
      model_id: modelId,
      provider_id: providerId,
      name: (data.name as string) || (data.full_name as string),
      description: (data.description as string) || '',
      created: Math.floor(Date.now() / 1000),
      context_length: (data.contextLength as number) || 0,
      architecture: {
        modality: (data.modelType as string) || 'text',
        input_modalities: [(data.modelType as string) || 'text'],
        output_modalities: [(data.modelType as string) || 'text'],
        tokenizer: '',
        instruct_type: null,
      },
      pricing: this.convertPricingToPerToken(pricing),
      per_request_limits: null,
      top_provider: null,
      enabled: data.isEnabled !== false,
    };

    const created = await apiClient.post<AdminModel>(
      '/admin/api/models/create',
      payload
    );

    return this.transformAdminModelToModel(
      {
        ...created,
        pricing: this.convertPricingToPerMillionTokens(created.pricing),
      },
      data.provider as string
    );
  }

  static async updateModel(
    modelId: string,
    data: Record<string, unknown>
  ): Promise<AdminModelAsModel> {
    const providerId = data.provider_id
      ? parseInt(data.provider_id as string)
      : null;

    const existingModel = await this.getModel(modelId, providerId);

    const pricing = {
      prompt: (data.input_cost as number) / 1000000,
      completion: (data.output_cost as number) / 1000000,
      request: (data.min_cost_per_request as number) || 0,
      image: 0,
      web_search: 0,
      internal_reasoning: 0,
    };

    const payload: AdminModel & {
      model_id: string;
      provider_id: number | null;
    } = {
      ...existingModel,
      model_id: modelId,
      provider_id: providerId,
      pricing,
    };

    if (data.name) payload.name = data.name as string;
    if (data.description) payload.description = data.description as string;
    if (data.contextLength !== undefined)
      payload.context_length = data.contextLength as number;
    if (data.isEnabled !== undefined)
      payload.enabled = data.isEnabled as boolean;

    const updated = await apiClient.post<AdminModel>(
      '/admin/api/models/update',
      {
        ...payload,
        pricing: this.convertPricingToPerToken(payload.pricing),
      }
    );

    return this.transformAdminModelToModel(
      {
        ...updated,
        pricing: this.convertPricingToPerMillionTokens(updated.pricing),
      },
      data.provider as string
    );
  }

  static async deleteModel(
    modelId: string,
    providerId?: string
  ): Promise<{ message: string }> {
    const providerIdNum = providerId ? parseInt(providerId) : null;
    await apiClient.post('/admin/api/models/delete', {
      model_id: modelId,
      provider_id: providerIdNum,
    });
    return { message: 'Model deleted successfully' };
  }

  static async softDeleteModel(
    modelId: string,
    providerId?: string
  ): Promise<{ message: string }> {
    const providerIdNum = providerId ? parseInt(providerId) : null;
    const model = await this.getModel(modelId, providerIdNum);

    await apiClient.post('/admin/api/models/update', {
      model_id: modelId,
      provider_id: providerIdNum,
      ...model,
      enabled: false,
      pricing: this.convertPricingToPerToken(model.pricing),
    });

    return { message: 'Model soft deleted successfully' };
  }

  static async deleteModels(
    modelIds: string[],
    providerId?: string
  ): Promise<{ deleted_count: number; message: string }> {
    if (!providerId) {
      throw new Error('provider_id is required to delete models');
    }
    const providerIdNum = parseInt(providerId);
    for (const id of modelIds) {
      await this.deleteProviderModel(providerIdNum, id);
    }
    return {
      deleted_count: modelIds.length,
      message: 'Models deleted successfully',
    };
  }

  static async softDeleteModels(
    modelIds: string[],
    providerId?: string
  ): Promise<{ deleted_count: number; message: string }> {
    if (!providerId) {
      throw new Error('provider_id is required to soft delete models');
    }
    const providerIdNum = parseInt(providerId);
    for (const id of modelIds) {
      const model = await this.getProviderModel(providerIdNum, id);
      await this.updateProviderModel(providerIdNum, id, {
        ...model,
        enabled: false,
      });
    }
    return {
      deleted_count: modelIds.length,
      message: 'Models soft deleted successfully',
    };
  }

  static async bulkUpdateModels(
    modelIds: string[],
    updates: { api_key?: string; url?: string },
    providerId?: string
  ): Promise<{
    updated_count: number;
    total_count: number;
    message: string;
    errors: string[];
  }> {
    if (!providerId) {
      throw new Error('provider_id is required for bulk updates');
    }

    const errors: string[] = [];
    let updated_count = 0;
    const providerIdNum = parseInt(providerId);

    console.log('Bulk update not implemented, ignoring updates:', updates);

    for (const id of modelIds) {
      try {
        const model = await this.getProviderModel(providerIdNum, id);
        await this.updateProviderModel(providerIdNum, id, model);
        updated_count++;
      } catch (error: unknown) {
        const errorMessage =
          error instanceof Error ? error.message : 'Unknown error';
        errors.push(`Failed to update ${id}: ${errorMessage}`);
      }
    }

    return {
      updated_count,
      total_count: modelIds.length,
      message: 'Bulk update completed',
      errors,
    };
  }

  static async deleteAllModels(): Promise<{
    deleted_count: number;
    message: string;
  }> {
    const providers = await this.getUpstreamProviders();
    let totalDeleted = 0;

    for (const provider of providers) {
      const result = await this.deleteAllProviderModels(provider.id);
      totalDeleted += result.deleted;
    }

    return {
      deleted_count: totalDeleted,
      message: 'All models deleted successfully',
    };
  }

  static async deleteModelsByProvider(
    providerId: string
  ): Promise<{ deleted_count: number; message: string }> {
    const result = await this.deleteAllProviderModels(parseInt(providerId));
    return {
      deleted_count: result.deleted,
      message: 'Provider models deleted successfully',
    };
  }

  static async restoreModels(
    modelIds: string[],
    providerId?: string
  ): Promise<{ restored_count: number; message: string }> {
    if (!providerId) {
      throw new Error('provider_id is required to restore models');
    }
    const providerIdNum = parseInt(providerId);
    for (const id of modelIds) {
      const model = await this.getProviderModel(providerIdNum, id);
      await this.updateProviderModel(providerIdNum, id, {
        ...model,
        enabled: true,
      });
    }
    return {
      restored_count: modelIds.length,
      message: 'Models restored successfully',
    };
  }

  static async refreshAllModels(): Promise<{ message: string }> {
    return { message: 'Refresh not implemented for admin API' };
  }

  static async refreshModels(data: {
    provider_id: string;
  }): Promise<{ message: string }> {
    console.log(
      'Refresh models not implemented for admin API, ignoring data:',
      data
    );
    return { message: 'Refresh not implemented for admin API' };
  }

  static async getOpenRouterPresets(): Promise<AdminModel[]> {
    const presets = await apiClient.get<AdminModel[]>(
      '/admin/api/openrouter-presets'
    );
    return presets.map((m) => ({
      ...m,
      pricing: this.convertPricingToPerMillionTokens(m.pricing),
    }));
  }

  static async getSettings(): Promise<Record<string, unknown>> {
    return await apiClient.get<Record<string, unknown>>('/admin/api/settings');
  }

  static async updateSettings(
    settings: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    return await apiClient.patch<Record<string, unknown>>(
      '/admin/api/settings',
      settings
    );
  }

  static async login(password: string): Promise<{
    ok: boolean;
    token: string;
    expires_in: number;
  }> {
    return await apiClient.post<{
      ok: boolean;
      token: string;
      expires_in: number;
    }>('/admin/api/login', { password });
  }

  static async logout(): Promise<{ ok: boolean }> {
    return await apiClient.post<{ ok: boolean }>('/admin/api/logout', {});
  }
}
