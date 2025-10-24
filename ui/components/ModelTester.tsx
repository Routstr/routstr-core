'use client';

import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { type Model } from '@/lib/api/schemas/models';
import { ModelService } from '@/lib/api/services/models';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Loader2,
  Send,
  CheckCircle,
  XCircle,
  Info,
  Key,
  Globe,
} from 'lucide-react';
import { toast } from 'sonner';

interface ModelTesterProps {
  models: Model[];
}

interface ChatCompletionRequest {
  model: string;
  messages: {
    role: 'system' | 'user' | 'assistant';
    content: string;
  }[];
  max_tokens?: number;
  temperature?: number;
}

interface ChatCompletionResponse {
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

const DEFAULT_SYSTEM_MESSAGE =
  'You are a helpful assistant. Please respond concisely.';
const DEFAULT_USER_MESSAGE =
  'Hello! Can you tell me what model you are and confirm that you are working correctly?';

export function ModelTester({ models }: ModelTesterProps) {
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [systemMessage, setSystemMessage] = useState(DEFAULT_SYSTEM_MESSAGE);
  const [userMessage, setUserMessage] = useState(DEFAULT_USER_MESSAGE);
  const [maxTokens, setMaxTokens] = useState<number>(150);
  const [temperature, setTemperature] = useState<number>(0.7);
  const [response, setResponse] = useState<ChatCompletionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Fetch model groups for API key resolution
  const { data: groups = [] } = useQuery({
    queryKey: ['model-groups'],
    queryFn: () => ModelService.getModelGroups(),
    refetchOnWindowFocus: false,
  });

  const selectedModel = models.find((model) => model.id === selectedModelId);

  // Get effective API key and endpoint URL for the selected model
  const getModelCredentials = (model: Model) => {
    const group = groups.find((g) => g.provider === model.provider);

    // Determine API key (individual takes precedence over group)
    const apiKey = model.api_key || group?.group_api_key;

    // Determine endpoint URL
    let endpointUrl = model.url;

    // If model URL is relative and group has a base URL, combine them
    if (model.url.startsWith('/') && group?.group_url) {
      endpointUrl = `${group.group_url.replace(/\/$/, '')}${model.url}`;
    }

    // Ensure the URL ends with /chat/completions for chat models
    if (
      model.modelType === 'text' &&
      !endpointUrl.includes('/chat/completions')
    ) {
      endpointUrl = endpointUrl.replace(/\/$/, '') + '/chat/completions';
    }

    return {
      apiKey,
      endpointUrl,
      group,
    };
  };

  const testModelMutation = useMutation({
    mutationFn: async (request: ChatCompletionRequest) => {
      if (!selectedModel) {
        throw new Error('No model selected');
      }

      setError(null);
      setResponse(null);

      try {
        console.log(`Testing model via proxy: ${selectedModel.name}`);
        console.log('Request payload:', request);

        const response = await ModelService.testModel(
          selectedModel.id,
          'chat-completions',
          request as unknown as Record<string, unknown>
        );

        if (!response.success) {
          throw new Error(response.error || 'Test failed');
        }

        return response.data as ChatCompletionResponse;
      } catch (err: unknown) {
        console.error('Model test error via proxy:', err);

        const errorMessage =
          err instanceof Error ? err.message : 'Failed to test model via proxy';
        throw new Error(errorMessage);
      }
    },
    onSuccess: (data) => {
      setResponse(data);
      toast.success('Model test completed successfully!');
    },
    onError: (err: Error) => {
      const errorMessage = err?.message || 'Unknown error occurred';
      setError(errorMessage);
      toast.error(`Model test failed: ${errorMessage}`);
    },
  });

  const handleTest = async () => {
    if (!selectedModel) {
      toast.error('Please select a model to test');
      return;
    }

    if (!userMessage.trim()) {
      toast.error('Please enter a test message');
      return;
    }

    const messages = [];

    if (systemMessage.trim()) {
      messages.push({
        role: 'system' as const,
        content: systemMessage.trim(),
      });
    }

    messages.push({
      role: 'user' as const,
      content: userMessage.trim(),
    });

    const request: ChatCompletionRequest = {
      model: selectedModel.name,
      messages,
      max_tokens: maxTokens,
      temperature: temperature,
    };

    testModelMutation.mutate(request);
  };

  const enabledModels = models.filter((model) => model.isEnabled);
  const credentials = selectedModel ? getModelCredentials(selectedModel) : null;

  return (
    <Card className='w-full'>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <Send className='h-5 w-5' />
          Model Credential Tester
        </CardTitle>
        <CardDescription>
          Test model functionality by sending chat completion requests through
          the secure proxy (resolves CORS and network issues)
        </CardDescription>
      </CardHeader>
      <CardContent className='space-y-6'>
        {/* Model Selection */}
        <div className='space-y-2'>
          <Label htmlFor='model-select'>Select Model</Label>
          <Select value={selectedModelId} onValueChange={setSelectedModelId}>
            <SelectTrigger id='model-select'>
              <SelectValue placeholder='Choose a model to test...' />
            </SelectTrigger>
            <SelectContent>
              {enabledModels.map((model) => (
                <SelectItem key={model.id} value={model.id}>
                  <div className='flex items-center gap-2'>
                    <span>{model.name}</span>
                    <Badge variant='outline' className='text-xs'>
                      {model.provider}
                    </Badge>
                    {model.is_free && (
                      <Badge variant='secondary' className='text-xs'>
                        Free
                      </Badge>
                    )}
                  </div>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {selectedModel && credentials && (
            <div className='text-muted-foreground bg-muted space-y-2 rounded-md p-3 text-sm'>
              <div className='flex items-center gap-2'>
                <Globe className='h-4 w-4' />
                <span>
                  <strong>Endpoint:</strong> {credentials.endpointUrl}
                </span>
              </div>
              <div className='flex items-center gap-2'>
                <Key className='h-4 w-4' />
                <span>
                  <strong>API Key:</strong>{' '}
                  {credentials.apiKey
                    ? `${credentials.apiKey.substring(0, 8)}...`
                    : 'Not configured'}
                </span>
                <Badge
                  variant={credentials.apiKey ? 'default' : 'destructive'}
                  className='text-xs'
                >
                  {selectedModel.api_key_type || 'Unknown'}
                </Badge>
              </div>
              <div>
                <span>
                  <strong>Provider:</strong> {selectedModel.provider}
                </span>
              </div>
              <div>
                <span>
                  <strong>Type:</strong> {selectedModel.modelType}
                </span>
              </div>
              {selectedModel.contextLength && (
                <div>
                  <span>
                    <strong>Context Length:</strong>{' '}
                    {selectedModel.contextLength.toLocaleString()}
                  </span>
                </div>
              )}
              {!credentials.apiKey && (
                <Alert variant='default' className='mt-2'>
                  <Info className='h-4 w-4' />
                  <AlertDescription>
                    No API key configured for this model. Testing may still work
                    if the model is free or if authentication is handled
                    elsewhere. For models requiring authentication, please add
                    an API key to the model or its provider group.
                  </AlertDescription>
                </Alert>
              )}
            </div>
          )}
        </div>

        {/* Test Parameters */}
        <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
          <div className='space-y-2'>
            <Label htmlFor='max-tokens'>Max Tokens</Label>
            <input
              id='max-tokens'
              type='number'
              min={1}
              max={4000}
              value={maxTokens}
              onChange={(e) => setMaxTokens(parseInt(e.target.value) || 150)}
              className='border-input bg-background w-full rounded-md border px-3 py-2 text-sm'
            />
          </div>
          <div className='space-y-2'>
            <Label htmlFor='temperature'>Temperature</Label>
            <input
              id='temperature'
              type='number'
              min={0}
              max={2}
              step={0.1}
              value={temperature}
              onChange={(e) =>
                setTemperature(parseFloat(e.target.value) || 0.7)
              }
              className='border-input bg-background w-full rounded-md border px-3 py-2 text-sm'
            />
          </div>
        </div>

        {/* System Message */}
        <div className='space-y-2'>
          <Label htmlFor='system-message'>System Message (Optional)</Label>
          <Textarea
            id='system-message'
            placeholder='Enter system message...'
            value={systemMessage}
            onChange={(e) => setSystemMessage(e.target.value)}
            rows={2}
          />
        </div>

        {/* User Message */}
        <div className='space-y-2'>
          <Label htmlFor='user-message'>Test Message</Label>
          <Textarea
            id='user-message'
            placeholder='Enter your test message...'
            value={userMessage}
            onChange={(e) => setUserMessage(e.target.value)}
            rows={3}
          />
        </div>

        {/* Test Button */}
        <Button
          onClick={handleTest}
          disabled={!selectedModelId || testModelMutation.isPending}
          className='w-full'
        >
          {testModelMutation.isPending ? (
            <>
              <Loader2 className='mr-2 h-4 w-4 animate-spin' />
              Testing Model...
            </>
          ) : (
            <>
              <Send className='mr-2 h-4 w-4' />
              Test Model (via Proxy)
            </>
          )}
        </Button>

        {/* Results */}
        {error && (
          <Alert variant='destructive'>
            <XCircle className='h-4 w-4' />
            <AlertDescription>
              <strong>Test Failed:</strong> {error}
            </AlertDescription>
          </Alert>
        )}

        {response && (
          <Alert>
            <CheckCircle className='h-4 w-4' />
            <AlertDescription>
              <strong>Test Successful!</strong> Model responded correctly via
              secure proxy.
            </AlertDescription>
          </Alert>
        )}

        {response && (
          <div className='space-y-4'>
            <div className='space-y-2'>
              <Label>Model Response</Label>
              <div className='bg-muted rounded-md p-4'>
                <p className='text-sm whitespace-pre-wrap'>
                  {response.choices[0]?.message?.content ||
                    'No content in response'}
                </p>
              </div>
            </div>

            {response.usage && (
              <div className='space-y-2'>
                <Label>Usage Statistics</Label>
                <div className='grid grid-cols-3 gap-4 text-sm'>
                  <div className='bg-muted rounded p-2 text-center'>
                    <div className='font-semibold'>
                      {response.usage.prompt_tokens}
                    </div>
                    <div className='text-muted-foreground'>Prompt Tokens</div>
                  </div>
                  <div className='bg-muted rounded p-2 text-center'>
                    <div className='font-semibold'>
                      {response.usage.completion_tokens}
                    </div>
                    <div className='text-muted-foreground'>
                      Completion Tokens
                    </div>
                  </div>
                  <div className='bg-muted rounded p-2 text-center'>
                    <div className='font-semibold'>
                      {response.usage.total_tokens}
                    </div>
                    <div className='text-muted-foreground'>Total Tokens</div>
                  </div>
                </div>
              </div>
            )}

            <div className='space-y-2'>
              <Label>Raw Response</Label>
              <details className='group'>
                <summary className='text-muted-foreground hover:text-foreground cursor-pointer text-sm'>
                  <Info className='mr-1 inline h-4 w-4' />
                  Show detailed response data
                </summary>
                <pre className='bg-muted mt-2 max-h-60 overflow-auto rounded-md p-4 text-xs'>
                  {JSON.stringify(response, null, 2)}
                </pre>
              </details>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
