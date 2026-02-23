import Image from 'next/image';
import { Label } from '@/components/ui/label';

export interface ChatCompletionResponse {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: {
    index: number;
    message: {
      role: string;
      content: string;
    };
    finish_reason: string;
  }[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

export interface EmbeddingResponse {
  object: string;
  data: {
    object: string;
    index: number;
    embedding: number[];
  }[];
  model: string;
  usage: {
    prompt_tokens: number;
    total_tokens: number;
  };
}

export interface ImageGenerationResponse {
  created: number;
  data: {
    url: string;
    revised_prompt?: string;
  }[];
}

export interface AudioResponse {
  type: 'audio';
  url: string;
  size: number;
}

export interface AudioTranscriptionResponse {
  text: string;
}

export interface ModelsListResponse {
  object: string;
  data: {
    id: string;
    object?: string;
    created?: number;
  }[];
}

export type ApiResponse =
  | ChatCompletionResponse
  | EmbeddingResponse
  | ImageGenerationResponse
  | AudioResponse
  | AudioTranscriptionResponse
  | ModelsListResponse;

const isAudioResponse = (response: ApiResponse): response is AudioResponse => {
  return 'type' in response && response.type === 'audio';
};

const isChatCompletionResponse = (
  response: ApiResponse
): response is ChatCompletionResponse => {
  return 'choices' in response;
};

const isEmbeddingResponse = (
  response: ApiResponse
): response is EmbeddingResponse => {
  return (
    'data' in response &&
    Array.isArray(response.data) &&
    response.data.length > 0 &&
    'embedding' in response.data[0]
  );
};

const isImageGenerationResponse = (
  response: ApiResponse
): response is ImageGenerationResponse => {
  return (
    'data' in response &&
    Array.isArray(response.data) &&
    response.data.length > 0 &&
    'url' in response.data[0]
  );
};

const isModelsListResponse = (
  response: ApiResponse
): response is ModelsListResponse => {
  return (
    'data' in response &&
    Array.isArray(response.data) &&
    response.data.length > 0 &&
    'id' in response.data[0]
  );
};

const isAudioTranscriptionResponse = (
  response: ApiResponse
): response is AudioTranscriptionResponse => {
  return 'text' in response;
};

export function ApiEndpointResponse({ response }: { response: ApiResponse | null }) {
  if (!response) {
    return null;
  }

  if (isAudioResponse(response)) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Label>Generated Audio</Label>
          <div className='bg-muted rounded-md p-4'>
            <audio controls src={response.url} className='w-full'>
              Your browser does not support the audio element.
            </audio>
            <p className='text-muted-foreground mt-2 text-sm'>
              Audio file size: {(response.size / 1024).toFixed(2)} KB
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (isAudioTranscriptionResponse(response)) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Label>Transcription Result</Label>
          <div className='bg-muted rounded-md p-4'>
            <p className='text-sm whitespace-pre-wrap'>
              {response.text || 'No transcription result available'}
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (isChatCompletionResponse(response)) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Label>Model Response</Label>
          <div className='bg-muted rounded-md p-4'>
            <p className='text-sm whitespace-pre-wrap'>
              {response.choices?.[0]?.message?.content || 'No content in response'}
            </p>
          </div>
        </div>

        {response.usage && (
          <div className='space-y-2'>
            <Label>Usage Statistics</Label>
            <div className='grid grid-cols-1 gap-2 text-sm sm:grid-cols-3 sm:gap-4'>
              <div className='bg-muted rounded p-2 text-center'>
                <div className='font-semibold'>{response.usage.prompt_tokens}</div>
                <div className='text-muted-foreground'>Prompt Tokens</div>
              </div>
              <div className='bg-muted rounded p-2 text-center'>
                <div className='font-semibold'>{response.usage.completion_tokens}</div>
                <div className='text-muted-foreground'>Completion Tokens</div>
              </div>
              <div className='bg-muted rounded p-2 text-center'>
                <div className='font-semibold'>{response.usage.total_tokens}</div>
                <div className='text-muted-foreground'>Total Tokens</div>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  if (isEmbeddingResponse(response)) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Label>Embedding Vector</Label>
          <div className='bg-muted rounded-md p-4'>
            <p className='text-muted-foreground mb-2 text-sm'>
              Generated {response.data?.[0]?.embedding?.length || 0} dimensional
              embedding vector
            </p>
            <details className='group'>
              <summary className='hover:text-foreground cursor-pointer text-sm'>
                Show first 10 values
              </summary>
              <pre className='mt-2 text-xs'>
                {JSON.stringify(response.data?.[0]?.embedding?.slice(0, 10), null, 2)}
                ...
              </pre>
            </details>
          </div>
        </div>

        {response.usage && (
          <div className='space-y-2'>
            <Label>Usage Statistics</Label>
            <div className='bg-muted rounded p-2 text-center text-sm'>
              <div className='font-semibold'>{response.usage.total_tokens}</div>
              <div className='text-muted-foreground'>Total Tokens</div>
            </div>
          </div>
        )}
      </div>
    );
  }

  if (isImageGenerationResponse(response)) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Label>Generated Images</Label>
          <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
            {response.data?.map((image, index: number) => (
              <div key={index} className='space-y-2'>
                <div className='relative aspect-square w-full'>
                  <Image
                    src={image.url}
                    alt={`Generated image ${index + 1}`}
                    fill
                    className='rounded-md border object-cover'
                    unoptimized={true}
                  />
                </div>
                {image.revised_prompt && (
                  <p className='text-muted-foreground text-xs'>
                    Revised prompt: {image.revised_prompt}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (isModelsListResponse(response)) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Label>Available Models</Label>
          <div className='max-h-60 overflow-auto'>
            <div className='grid gap-2'>
              {response.data?.map((model) => (
                <div key={model.id} className='bg-muted rounded-md p-3'>
                  <div className='font-medium'>{model.id}</div>
                  {model.object && (
                    <div className='text-muted-foreground text-sm'>
                      Type: {model.object}
                    </div>
                  )}
                  {model.created && (
                    <div className='text-muted-foreground text-sm'>
                      Created: {new Date(model.created * 1000).toLocaleDateString()}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  return null;
}
