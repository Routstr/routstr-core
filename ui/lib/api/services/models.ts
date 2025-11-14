import { apiClient } from '../client';
import { Model, CreateModel, UpdateModel } from '../schemas/models';
import { z } from 'zod';

// Model group schemas matching backend
export const ModelGroupSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  provider: z.string(),
  group_api_key: z.string().optional(),
  group_url: z.string().optional(),
});

export const CreateModelGroupSchema = z.object({
  provider: z.string(),
  group_api_key: z.string().optional(),
  group_url: z.string().optional(),
});

export const UpdateModelGroupSchema = z.object({
  provider: z.string().optional(),
  group_api_key: z.string().optional(),
  group_url: z.string().optional(),
});

export const CollectModelsRequestSchema = z.object({
  base_endpoint: z.string(),
  api_key: z.string().optional(),
  provider_name: z.string(),
  default_input_cost: z.number(),
  default_output_cost: z.number(),
  default_min_cost: z.number(),
});

export const CollectModelsResponseSchema = z.object({
  collected_count: z.number(),
  skipped_count: z.number(),
  models: z.array(z.any()),
  errors: z.array(z.string()),
});

export const RefreshModelsRequestSchema = z.object({
  provider_id: z.string(),
});

export const RefreshModelsResponseSchema = z.object({
  refreshed_count: z.number(),
  created_count: z.number(),
  restored_count: z.number(),
  deleted_count: z.number(),
  errors: z.array(z.string()),
});

export const RefreshAllModelsResponseSchema = z.object({
  total_refreshed_count: z.number(),
  total_created_count: z.number(),
  total_restored_count: z.number(),
  total_deleted_count: z.number(),
  provider_results: z.array(
    z.object({
      provider_name: z.string(),
      refreshed_count: z.number(),
      created_count: z.number(),
      restored_count: z.number(),
      deleted_count: z.number(),
      errors: z.array(z.string()),
    })
  ),
  errors: z.array(z.string()),
});

export type ModelGroup = z.infer<typeof ModelGroupSchema>;
export type CreateModelGroup = z.infer<typeof CreateModelGroupSchema>;
export type UpdateModelGroup = z.infer<typeof UpdateModelGroupSchema>;
export type CollectModelsRequest = z.infer<typeof CollectModelsRequestSchema>;
export type CollectModelsResponse = z.infer<typeof CollectModelsResponseSchema>;
export type RefreshModelsRequest = z.infer<typeof RefreshModelsRequestSchema>;
export type RefreshModelsResponse = z.infer<typeof RefreshModelsResponseSchema>;
export type RefreshAllModelsResponse = z.infer<
  typeof RefreshAllModelsResponseSchema
>;

// Enhanced models schema with provider URL
export const EnhancedOpenAIModelSchema = z.object({
  id: z.string(),
  canonical_slug: z.string().optional(),
  hugging_face_id: z.string().optional(),
  name: z.string().optional(),
  created: z.number(),
  description: z.string().optional(),
  context_length: z.number().optional(),
  architecture: z.any().optional(),
  pricing: z.any().optional(),
  top_provider: z.any().optional(),
  per_request_limits: z.any().optional(),
  supported_parameters: z.array(z.string()).optional(),
  provider_url: z.string(),
  provider_name: z.string().optional(),
  group_id: z.string().optional(),
});

export const EnhancedModelListSchema = z.object({
  object: z.string(),
  data: z.array(EnhancedOpenAIModelSchema),
});

export type EnhancedOpenAIModel = z.infer<typeof EnhancedOpenAIModelSchema>;
export type EnhancedModelList = z.infer<typeof EnhancedModelListSchema>;

// Backend model structure
export const BackendModelSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  full_name: z.string(),
  name: z.string(),
  url: z.string(),
  input_cost: z.string(), // BigDecimal as string
  output_cost: z.string(), // BigDecimal as string
  api_key: z.string().optional(),
  min_cash_per_request: z.string(), // BigDecimal as string
  min_cost_per_request: z.string().optional(), // BigDecimal as string
  provider_id: z.string().optional(),
  provider: z.string().optional(),
  soft_deleted: z.boolean().optional(),
  architecture: z.any().optional(),
  model_type: z.string().optional(),
  description: z.string().optional(),
  context_length: z.number().optional(),
  is_free: z.boolean().optional(),
  // API key type indicators from backend
  has_own_api_key: z.boolean(),
  api_key_type: z.string(),
});

export type BackendModel = z.infer<typeof BackendModelSchema>;

// Transform backend model to frontend model
function transformBackendModelToFrontend(
  backendModel: BackendModel,
  providerName?: string
): Model {
  return {
    id: backendModel.id,
    name: backendModel.name,
    full_name: backendModel.full_name,
    description: backendModel.description,
    modelType: backendModel.model_type || 'text',
    isEnabled: !backendModel.soft_deleted,
    createdAt: backendModel.created_at,
    updatedAt: backendModel.updated_at,
    provider: backendModel.provider || providerName || '',
    url: backendModel.url,
    api_key: backendModel.api_key,
    input_cost: Number(backendModel.input_cost),
    output_cost: Number(backendModel.output_cost),
    min_cost_per_request: Number(backendModel.min_cost_per_request || '0'),
    min_cash_per_request: Number(backendModel.min_cash_per_request || '0'),
    contextLength: backendModel.context_length,
    apiKeyRequired: true,
    provider_id: backendModel.provider_id,
    is_free: backendModel.is_free ?? false,
    soft_deleted: backendModel.soft_deleted ?? false,
    has_own_api_key: backendModel.has_own_api_key,
    api_key_type: backendModel.api_key_type,
  };
}

export class ModelService {
  // Model Group operations
  static async createModelGroup(data: CreateModelGroup): Promise<ModelGroup> {
    return await apiClient.post<ModelGroup>('/api/model-groups', {
      provider: data.provider,
      group_api_key: data.group_api_key,
      group_url: data.group_url,
    });
  }

  static async getModelGroups(): Promise<ModelGroup[]> {
    return await apiClient.get<ModelGroup[]>('/api/model-groups');
  }

  static async getModelGroup(id: string): Promise<ModelGroup> {
    return await apiClient.get<ModelGroup>(`/api/model-groups/${id}`);
  }

  static async updateModelGroup(
    id: string,
    data: UpdateModelGroup
  ): Promise<ModelGroup> {
    return await apiClient.put<ModelGroup>(`/api/model-groups/${id}`, {
      provider: data.provider,
      group_api_key: data.group_api_key,
      group_url: data.group_url,
    });
  }

  static async deleteModelGroup(id: string): Promise<{ message: string }> {
    return await apiClient.delete<{ message: string }>(
      `/api/model-groups/${id}`
    );
  }

  // Model operations
  static async createModel(data: CreateModel): Promise<Model> {
    const backendData = {
      full_name: data.name, // Use name as full_name for manually created models
      name: data.name,
      url: data.url,
      api_key: data.api_key,
      input_cost: data.input_cost,
      output_cost: data.output_cost,
      min_cost_per_request: data.min_cost_per_request,
      min_cash_per_request: data.min_cash_per_request,
      provider: data.provider,
      model_type: data.modelType,
      description: data.description,
      context_length: data.contextLength,
      is_free: data.is_free,
    };

    const backendModel = await apiClient.post<BackendModel>(
      '/api/models',
      backendData
    );
    return transformBackendModelToFrontend(backendModel);
  }

  static async getModels(): Promise<Model[]> {
    const backendModels = await apiClient.get<BackendModel[]>('/api/models');
    return backendModels.map((model) => transformBackendModelToFrontend(model));
  }

  static async getModel(id: string): Promise<Model> {
    const backendModel = await apiClient.get<BackendModel>(`/api/models/${id}`);
    return transformBackendModelToFrontend(backendModel);
  }

  static async getModelsByProvider(providerId: string): Promise<Model[]> {
    const backendModels = await apiClient.get<BackendModel[]>(
      `/api/models/provider/${providerId}`
    );
    return backendModels.map((model) => transformBackendModelToFrontend(model));
  }

  static async updateModel(modelId: string, data: UpdateModel): Promise<Model> {
    const backendData = {
      name: data.name,
      url: data.url,
      api_key: data.api_key,
      input_cost: data.input_cost,
      output_cost: data.output_cost,
      min_cost_per_request: data.min_cost_per_request,
      min_cash_per_request: data.min_cash_per_request,
      provider: data.provider,
      model_type: data.modelType,
      description: data.description,
      context_length: data.contextLength,
      is_free: data.is_free,
    };

    const backendModel = await apiClient.put<BackendModel>(
      `/api/models/${modelId}`,
      backendData
    );
    return transformBackendModelToFrontend(backendModel);
  }

  static async deleteModel(id: string): Promise<{ message: string }> {
    return await apiClient.delete<{ message: string }>(`/api/models/${id}`);
  }

  static async softDeleteModel(id: string): Promise<{ message: string }> {
    return await apiClient.put<{ message: string }>(
      `/api/models/${id}/soft-delete`,
      {}
    );
  }

  // Bulk deletion methods
  static async deleteModels(
    modelIds: string[]
  ): Promise<{ deleted_count: number; message: string }> {
    return await apiClient.post<{ deleted_count: number; message: string }>(
      '/api/models/bulk/delete',
      {
        model_ids: modelIds,
      }
    );
  }

  static async softDeleteModels(
    modelIds: string[]
  ): Promise<{ deleted_count: number; message: string }> {
    return await apiClient.post<{ deleted_count: number; message: string }>(
      '/api/models/bulk/soft-delete',
      {
        model_ids: modelIds,
      }
    );
  }

  // Bulk update method
  static async bulkUpdateModels(
    modelIds: string[],
    updates: { api_key?: string; url?: string }
  ): Promise<{
    updated_count: number;
    total_count: number;
    message: string;
    errors: string[];
  }> {
    return await apiClient.post<{
      updated_count: number;
      total_count: number;
      message: string;
      errors: string[];
    }>('/api/models/bulk/update', {
      model_ids: modelIds,
      api_key: updates.api_key,
      url: updates.url,
    });
  }

  static async deleteAllModels(): Promise<{
    deleted_count: number;
    message: string;
  }> {
    return await apiClient.post<{ deleted_count: number; message: string }>(
      '/api/models/all/delete',
      {}
    );
  }

  static async deleteModelsByProvider(
    providerId: string
  ): Promise<{ deleted_count: number; message: string }> {
    return await apiClient.post<{ deleted_count: number; message: string }>(
      `/api/models/provider/${providerId}/delete`,
      {}
    );
  }

  static async restoreModels(
    modelIds: string[]
  ): Promise<{ restored_count: number; message: string }> {
    return await apiClient.post<{ restored_count: number; message: string }>(
      '/api/models/bulk/restore',
      {
        model_ids: modelIds,
      }
    );
  }

  static async getSoftDeletedModels(): Promise<Model[]> {
    const backendModels = await apiClient.get<BackendModel[]>(
      '/api/models/deleted'
    );
    return backendModels.map((model) => transformBackendModelToFrontend(model));
  }

  // Model collection
  static async collectModels(
    data: CollectModelsRequest
  ): Promise<CollectModelsResponse> {
    return await apiClient.post<CollectModelsResponse>(
      '/api/models/collect',
      data
    );
  }

  // Get models with provider information
  static async getModelsWithProviders(): Promise<{
    models: Model[];
    groups: ModelGroup[];
  }> {
    const [backendModels, groups] = await Promise.all([
      apiClient.get<BackendModel[]>('/api/models'),
      this.getModelGroups(),
    ]);

    // Map provider_id to provider name and transform models with provider names
    const groupsMap = new Map(groups.map((g) => [g.id, g.provider]));
    const enhancedModels = backendModels.map((model) =>
      transformBackendModelToFrontend(
        model,
        groupsMap.get(model.provider_id || '') || 'Unknown'
      )
    );

    return { models: enhancedModels, groups };
  }

  // Legacy method for compatibility
  static async listModels(options?: {
    team_id?: string;
    return_wildcard_routes?: boolean;
    enabled?: boolean;
  }): Promise<Model[]> {
    const { models } = await this.getModelsWithProviders();
    return options?.enabled !== false
      ? models.filter((m) => m.isEnabled)
      : models;
  }

  // Legacy method for compatibility
  static async getModelInfo(modelId: string): Promise<Model> {
    return await this.getModel(modelId);
  }

  // Refresh models using group credentials
  static async refreshModels(
    data: RefreshModelsRequest
  ): Promise<RefreshModelsResponse> {
    return await apiClient.post<RefreshModelsResponse>(
      '/api/models/refresh',
      data
    );
  }

  // Refresh all models using all group credentials
  static async refreshAllModels(): Promise<RefreshAllModelsResponse> {
    return await apiClient.post<RefreshAllModelsResponse>(
      '/api/models/refresh-all',
      {}
    );
  }

  // Enhanced models with provider URL information
  static async getEnhancedModels(): Promise<EnhancedModelList> {
    return await apiClient.get<EnhancedModelList>('/api/enhanced-models');
  }

  static async getEnhancedModelsByProvider(
    providerName: string
  ): Promise<EnhancedModelList> {
    return await apiClient.get<EnhancedModelList>(
      `/api/enhanced-models/${encodeURIComponent(providerName)}`
    );
  }

  // Download functions for JSON export
  static downloadModelsAsJson(
    data: EnhancedModelList | Model[] | unknown,
    filename: string = 'models.json'
  ) {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  static async downloadAllEnhancedModels() {
    try {
      const data = await this.getEnhancedModels();
      this.downloadModelsAsJson(data, 'enhanced-models-all.json');
    } catch (error) {
      console.error('Error downloading all enhanced models:', error);
      throw error;
    }
  }

  static async downloadEnhancedModelsByProvider(providerName: string) {
    try {
      const data = await this.getEnhancedModelsByProvider(providerName);
      this.downloadModelsAsJson(
        data,
        `enhanced-models-${providerName.toLowerCase()}.json`
      );
    } catch (error) {
      console.error(
        `Error downloading enhanced models for provider ${providerName}:`,
        error
      );
      throw error;
    }
  }

  // Test model through proxy to avoid CORS issues
  static async testModel(
    modelId: string,
    endpointType: string,
    requestData: Record<string, unknown>
  ): Promise<{
    success: boolean;
    data?: unknown;
    error?: string;
    status_code?: number;
  }> {
    const response = await apiClient.post<{
      success: boolean;
      data?: unknown;
      error?: string;
      status_code?: number;
    }>('/api/models/test', {
      model_id: modelId,
      endpoint_type: endpointType,
      request_data: requestData,
    });

    return response;
  }
}
