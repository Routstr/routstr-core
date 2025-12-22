import { apiClient } from '../client';
import { z } from 'zod';

export const ModelMappingSchema = z.object({
  from: z.string(),
  to: z.string(),
});

export const CreateModelMappingSchema = z.object({
  from: z.string(),
  to: z.string(),
});

export const UpdateModelMappingSchema = z.object({
  to: z.string(),
});

export const ModelMappingsResponseSchema = z.record(z.string());

export const ReloadMappingsResponseSchema = z.object({
  ok: z.boolean(),
  mappings: z.record(z.string()),
});

export type ModelMapping = z.infer<typeof ModelMappingSchema>;
export type CreateModelMapping = z.infer<typeof CreateModelMappingSchema>;
export type UpdateModelMapping = z.infer<typeof UpdateModelMappingSchema>;
export type ModelMappingsResponse = z.infer<typeof ModelMappingsResponseSchema>;
export type ReloadMappingsResponse = z.infer<
  typeof ReloadMappingsResponseSchema
>;

export class ModelMappingService {
  static async getModelMappings(): Promise<ModelMappingsResponse> {
    return await apiClient.get<ModelMappingsResponse>(
      '/admin/api/model-mappings'
    );
  }

  static async createModelMapping(
    data: CreateModelMapping
  ): Promise<ModelMappingsResponse> {
    return await apiClient.post<ModelMappingsResponse>(
      '/admin/api/model-mappings',
      {
        from: data.from,
        to: data.to,
      }
    );
  }

  static async updateModelMapping(
    fromModel: string,
    data: UpdateModelMapping
  ): Promise<ModelMappingsResponse> {
    return await apiClient.put<ModelMappingsResponse>(
      `/admin/api/model-mappings/${encodeURIComponent(fromModel)}`,
      {
        to: data.to,
      }
    );
  }

  static async deleteModelMapping(
    fromModel: string
  ): Promise<ModelMappingsResponse> {
    return await apiClient.delete<ModelMappingsResponse>(
      `/admin/api/model-mappings/${encodeURIComponent(fromModel)}`
    );
  }

  static async reloadModelMappings(): Promise<ReloadMappingsResponse> {
    return await apiClient.post<ReloadMappingsResponse>(
      '/admin/api/model-mappings/reload',
      {}
    );
  }
}
