import { apiClient } from '../client';
import { z } from 'zod';

export const UpstreamProviderSchema = z.object({
  id: z.number(),
  provider_type: z.string(),
  base_url: z.string(),
  api_key: z.string().optional(),
  api_version: z.string().nullable().optional(),
  enabled: z.boolean(),
});

export const CreateUpstreamProviderSchema = z.object({
  provider_type: z.string(),
  base_url: z.string(),
  api_key: z.string(),
  api_version: z.string().nullable().optional(),
  enabled: z.boolean().default(true),
});

export const UpdateUpstreamProviderSchema = z.object({
  provider_type: z.string().optional(),
  base_url: z.string().optional(),
  api_key: z.string().optional(),
  api_version: z.string().nullable().optional(),
  enabled: z.boolean().optional(),
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
      result.prompt = result.prompt * 1000;
    }
    if (typeof result.completion === 'number') {
      result.completion = result.completion * 1000;
    }
    return result;
  }

  static convertPricingToPerToken(
    pricing: Record<string, unknown>
  ): Record<string, unknown> {
    if (!pricing) return pricing;
    const result = { ...pricing };
    if (typeof result.prompt === 'number') {
      result.prompt = result.prompt;
    }
    if (typeof result.completion === 'number') {
      result.completion = result.completion;
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
    return {
      ...data,
      db_models: data.db_models.map((m) => ({
        ...m,
        pricing: this.convertPricingToPerMillionTokens(m.pricing),
      })),
      remote_models: data.remote_models.map((m) => ({
        ...m,
        pricing: this.convertPricingToPerMillionTokens(m.pricing),
      })),
    };
  }

  static async getAdminModels(): Promise<AdminModel[]> {
    const models = await apiClient.get<AdminModel[]>('/admin/api/models');
    return models.map((m) => ({
      ...m,
      pricing: this.convertPricingToPerMillionTokens(m.pricing),
    }));
  }

  static async getAdminModel(modelId: string): Promise<AdminModel> {
    const model = await apiClient.get<AdminModel>(
      `/admin/api/models/${encodeURIComponent(modelId)}`
    );
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async createAdminModel(data: AdminModel): Promise<AdminModel> {
    const payload = {
      ...data,
      pricing: this.convertPricingToPerToken(data.pricing),
    };
    const model = await apiClient.post<AdminModel>(
      '/admin/api/models',
      payload
    );
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async updateAdminModel(
    modelId: string,
    data: AdminModel
  ): Promise<AdminModel> {
    const payload = {
      ...data,
      pricing: this.convertPricingToPerToken(data.pricing),
    };
    const model = await apiClient.patch<AdminModel>(
      `/admin/api/models/${encodeURIComponent(modelId)}`,
      payload
    );
    return {
      ...model,
      pricing: this.convertPricingToPerMillionTokens(model.pricing),
    };
  }

  static async deleteAdminModel(
    modelId: string
  ): Promise<{ ok: boolean; deleted_id: string }> {
    return await apiClient.delete<{ ok: boolean; deleted_id: string }>(
      `/admin/api/models/${encodeURIComponent(modelId)}`
    );
  }

  static async deleteAllAdminModels(): Promise<{
    ok: boolean;
    deleted: string;
  }> {
    return await apiClient.delete<{ ok: boolean; deleted: string }>(
      '/admin/api/models'
    );
  }

  static async batchCreateModels(
    models: AdminModel[]
  ): Promise<{ created: number; skipped: number }> {
    const payload = {
      models: models.map((m) => ({
        ...m,
        pricing: this.convertPricingToPerToken(m.pricing),
      })),
    };
    return await apiClient.post<{ created: number; skipped: number }>(
      '/admin/api/models/batch',
      payload
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
          allModels.push({
            ...this.transformAdminModelToModel(dbModel, provider.provider_type),
            has_own_api_key: false,
            api_key_type: 'group',
          });
        });

        providerModels.remote_models.forEach((remoteModel) => {
          if (!seenModelIds.has(remoteModel.id)) {
            allModels.push({
              ...this.transformAdminModelToModel(
                remoteModel,
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
    const pricing = {
      prompt: (data.input_cost as number) / 1000000,
      completion: (data.output_cost as number) / 1000000,
      request: (data.min_cost_per_request as number) || 0,
      image: 0,
      web_search: 0,
      internal_reasoning: 0,
    };

    const adminModel: AdminModel = {
      id: (data.id as string) || (data.full_name as string),
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
      pricing,
      per_request_limits: null,
      top_provider: null,
      upstream_provider_id: data.provider_id
        ? parseInt(data.provider_id as string)
        : null,
      enabled: data.isEnabled !== false,
    };

    const created = await this.createAdminModel(adminModel);
    return this.transformAdminModelToModel(created, data.provider as string);
  }

  static async updateModel(
    modelId: string,
    data: Record<string, unknown>
  ): Promise<AdminModelAsModel> {
    const pricing = {
      prompt: (data.input_cost as number) / 1000000,
      completion: (data.output_cost as number) / 1000000,
      request: (data.min_cost_per_request as number) || 0,
    };

    const payload: Record<string, unknown> = {
      id: modelId,
      pricing,
    };

    if (data.name) payload.name = data.name;
    if (data.description) payload.description = data.description;
    if (data.contextLength !== undefined)
      payload.context_length = data.contextLength;
    if (data.provider_id)
      payload.upstream_provider_id = parseInt(data.provider_id as string);
    if (data.isEnabled !== undefined) payload.enabled = data.isEnabled;

    const updated = await apiClient.post<AdminModel>(
      '/admin/api/models/update',
      payload
    );

    return this.transformAdminModelToModel(updated, data.provider as string);
  }

  static async deleteModel(modelId: string): Promise<{ message: string }> {
    await this.deleteAdminModel(modelId);
    return { message: 'Model deleted successfully' };
  }

  static async softDeleteModel(modelId: string): Promise<{ message: string }> {
    await apiClient.post('/admin/api/models/update', {
      id: modelId,
      enabled: false,
    });
    return { message: 'Model soft deleted successfully' };
  }

  static async deleteModels(
    modelIds: string[]
  ): Promise<{ deleted_count: number; message: string }> {
    for (const id of modelIds) {
      await this.deleteAdminModel(id);
    }
    return {
      deleted_count: modelIds.length,
      message: 'Models deleted successfully',
    };
  }

  static async softDeleteModels(
    modelIds: string[]
  ): Promise<{ deleted_count: number; message: string }> {
    for (const id of modelIds) {
      const model = await this.getAdminModel(id);
      await this.updateAdminModel(id, { ...model, enabled: false });
    }
    return {
      deleted_count: modelIds.length,
      message: 'Models soft deleted successfully',
    };
  }

  static async bulkUpdateModels(
    modelIds: string[],
    updates: { api_key?: string; url?: string }
  ): Promise<{
    updated_count: number;
    total_count: number;
    message: string;
    errors: string[];
  }> {
    const errors: string[] = [];
    let updated_count = 0;

    console.log('Bulk update not implemented, ignoring updates:', updates);

    for (const id of modelIds) {
      try {
        const model = await this.getAdminModel(id);
        await this.updateAdminModel(id, model);
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
    const models = await this.getAdminModels();
    await this.deleteAllAdminModels();
    return {
      deleted_count: models.length,
      message: 'All models deleted successfully',
    };
  }

  static async deleteModelsByProvider(
    providerId: string
  ): Promise<{ deleted_count: number; message: string }> {
    const models = await this.getAdminModels();
    const providerModels = models.filter(
      (m) => m.upstream_provider_id === parseInt(providerId)
    );
    for (const model of providerModels) {
      await this.deleteAdminModel(model.id);
    }
    return {
      deleted_count: providerModels.length,
      message: 'Provider models deleted successfully',
    };
  }

  static async restoreModels(
    modelIds: string[]
  ): Promise<{ restored_count: number; message: string }> {
    for (const id of modelIds) {
      await apiClient.post('/admin/api/models/update', {
        id,
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
}
