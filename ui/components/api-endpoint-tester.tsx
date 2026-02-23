'use client';

import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { type Model } from '@/lib/api/schemas/models';
import { ModelService } from '@/lib/api/services/models';
import { Button } from '@/components/ui/button';
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
import {
  ApiEndpointResponse,
  type ApiResponse,
} from '@/components/api-endpoint-response';
import { ApiEndpointForm } from '@/components/api-endpoint-form';
import {
  API_ENDPOINTS,
  DEFAULT_REQUESTS,
  type EndpointType,
  type ChatCompletionRequest,
  type EmbeddingRequest,
  type ImageGenerationRequest,
  type AudioSpeechRequest,
  type AudioTranscriptionRequest,
  type EndpointRequestData,
} from '@/components/api-endpoint-types';

interface ApiEndpointTesterProps {
  models: Model[];
}

export function ApiEndpointTester({ models }: ApiEndpointTesterProps) {
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [selectedEndpoint, setSelectedEndpoint] =
    useState<EndpointType>('chat-completions');
  const [response, setResponse] = useState<ApiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Chat Completions state
  const [systemMessage, setSystemMessage] = useState(
    DEFAULT_REQUESTS['chat-completions'].systemMessage
  );
  const [userMessage, setUserMessage] = useState(
    DEFAULT_REQUESTS['chat-completions'].userMessage
  );
  const [maxTokens, setMaxTokens] = useState(
    DEFAULT_REQUESTS['chat-completions'].maxTokens
  );
  const [temperature, setTemperature] = useState(
    DEFAULT_REQUESTS['chat-completions'].temperature
  );

  // Vision Chat state
  const [visionSystemMessage, setVisionSystemMessage] = useState(
    DEFAULT_REQUESTS['vision-chat'].systemMessage
  );
  const [visionUserMessage, setVisionUserMessage] = useState(
    DEFAULT_REQUESTS['vision-chat'].userMessage
  );
  const [visionMaxTokens, setVisionMaxTokens] = useState(
    DEFAULT_REQUESTS['vision-chat'].maxTokens
  );
  const [visionTemperature, setVisionTemperature] = useState(
    DEFAULT_REQUESTS['vision-chat'].temperature
  );
  const [imageDetail, setImageDetail] = useState<'low' | 'high' | 'auto'>(
    DEFAULT_REQUESTS['vision-chat'].imageDetail
  );
  const [selectedImage, setSelectedImage] = useState<File | null>(null);
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);

  // Voice Recording state
  const [isRecording, setIsRecording] = useState(false);
  const [recordedAudio, setRecordedAudio] = useState<File | null>(null);
  const [recordingUrl, setRecordingUrl] = useState<string | null>(null);
  const [mediaRecorder, setMediaRecorder] = useState<MediaRecorder | null>(
    null
  );
  const [audioTranscriptionPrompt, setAudioTranscriptionPrompt] = useState(
    DEFAULT_REQUESTS['audio-transcription'].prompt
  );
  const [audioResponseFormat, setAudioResponseFormat] = useState<
    'json' | 'text' | 'srt' | 'verbose_json' | 'vtt'
  >(DEFAULT_REQUESTS['audio-transcription'].response_format);
  const [audioTemperature, setAudioTemperature] = useState(
    DEFAULT_REQUESTS['audio-transcription'].temperature
  );
  const [audioLanguage, setAudioLanguage] = useState(
    DEFAULT_REQUESTS['audio-transcription'].language
  );

  // Embeddings state
  const [embeddingInput, setEmbeddingInput] = useState(
    DEFAULT_REQUESTS.embeddings.input
  );
  const [encodingFormat, setEncodingFormat] = useState<'float' | 'base64'>(
    DEFAULT_REQUESTS.embeddings.encoding_format
  );

  // Image Generation state
  const [imagePrompt, setImagePrompt] = useState(
    DEFAULT_REQUESTS.images.prompt
  );
  const [imageCount, setImageCount] = useState(DEFAULT_REQUESTS.images.n);
  const [imageSize, setImageSize] = useState<
    '256x256' | '512x512' | '1024x1024' | '1792x1024' | '1024x1792'
  >(DEFAULT_REQUESTS.images.size);
  const [imageQuality, setImageQuality] = useState<'standard' | 'hd'>(
    DEFAULT_REQUESTS.images.quality
  );
  const [imageStyle, setImageStyle] = useState<'vivid' | 'natural'>(
    DEFAULT_REQUESTS.images.style
  );

  // Audio Speech state
  const [speechInput, setSpeechInput] = useState(
    DEFAULT_REQUESTS['audio-speech'].input
  );
  const [speechVoice, setSpeechVoice] = useState<
    'alloy' | 'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer'
  >(DEFAULT_REQUESTS['audio-speech'].voice);
  const [speechFormat, setSpeechFormat] = useState<
    'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm'
  >(DEFAULT_REQUESTS['audio-speech'].response_format);
  const [speechSpeed, setSpeechSpeed] = useState(
    DEFAULT_REQUESTS['audio-speech'].speed
  );

  // Fetch model groups for API key resolution
  const { data: groups = [] } = useQuery({
    queryKey: ['model-groups'],
    queryFn: () => ModelService.getModelGroups(),
    refetchOnWindowFocus: false,
  });

  const selectedModel = models.find((model) => model.id === selectedModelId);

  // Image upload handler
  const handleImageUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      if (file.type.startsWith('image/')) {
        setSelectedImage(file);
        const previewUrl = URL.createObjectURL(file);
        setImagePreviewUrl(previewUrl);
      } else {
        toast.error('Please select a valid image file');
      }
    }
  };

  // Audio file upload handler
  const handleAudioUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      if (file.type.startsWith('audio/')) {
        setRecordedAudio(file);
        const audioUrl = URL.createObjectURL(file);
        setRecordingUrl(audioUrl);
      } else {
        toast.error('Please select a valid audio file');
      }
    }
  };

  // Voice recording functions
  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      const chunks: BlobPart[] = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunks.push(event.data);
        }
      };

      recorder.onstop = () => {
        const blob = new Blob(chunks, { type: 'audio/webm' });
        const file = new File([blob], 'recording.webm', { type: 'audio/webm' });
        setRecordedAudio(file);
        const audioUrl = URL.createObjectURL(blob);
        setRecordingUrl(audioUrl);

        // Stop all tracks to release microphone
        stream.getTracks().forEach((track) => track.stop());
      };

      recorder.start();
      setMediaRecorder(recorder);
      setIsRecording(true);
      toast.success('Recording started');
    } catch {
      toast.error(
        'Failed to start recording. Please check microphone permissions.'
      );
    }
  };

  const stopRecording = () => {
    if (mediaRecorder) {
      mediaRecorder.stop();
      setMediaRecorder(null);
      setIsRecording(false);
      toast.success('Recording stopped');
    }
  };

  // Convert file to base64 for vision API
  const fileToBase64 = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.readAsDataURL(file);
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = (error) => reject(error);
    });
  };

  // Get effective API key and endpoint URL for the selected model
  const getModelCredentials = (model: Model) => {
    const group = groups.find((g) => g.provider === model.provider);

    // Determine API key (individual takes precedence over group)
    const apiKey = model.api_key || group?.group_api_key;

    // Determine base endpoint URL
    let baseUrl = model.url;

    // If model URL is relative and group has a base URL, combine them
    if (model.url.startsWith('/') && group?.group_url) {
      baseUrl = `${group.group_url.replace(/\/$/, '')}${model.url}`;
    }

    // Remove any existing endpoint path to get base URL
    baseUrl = baseUrl.replace(/\/v1\/.*$/, '').replace(/\/$/, '');

    return {
      apiKey,
      baseUrl,
      group,
    };
  };

  const buildEndpointUrl = (baseUrl: string, endpointPath: string) => {
    return `${baseUrl}${endpointPath}`;
  };

  const buildRequest = async (): Promise<EndpointRequestData> => {
    if (!selectedModel) return null;

    switch (selectedEndpoint) {
      case 'chat-completions':
        const messages = [];
        if (systemMessage.trim()) {
          messages.push({
            role: 'system' as const,
            content: systemMessage.trim(),
          });
        }
        messages.push({ role: 'user' as const, content: userMessage.trim() });

        return {
          model: selectedModel.name,
          messages,
          max_tokens: maxTokens,
          temperature: temperature,
        } as ChatCompletionRequest;

      case 'vision-chat':
        if (!selectedImage) {
          throw new Error('Please select an image for vision analysis');
        }

        const imageBase64 = await fileToBase64(selectedImage);
        const visionMessages = [];

        if (visionSystemMessage.trim()) {
          visionMessages.push({
            role: 'system' as const,
            content: visionSystemMessage.trim(),
          });
        }

        visionMessages.push({
          role: 'user' as const,
          content: [
            {
              type: 'text' as const,
              text: visionUserMessage.trim(),
            },
            {
              type: 'image_url' as const,
              image_url: {
                url: imageBase64,
                detail: imageDetail,
              },
            },
          ],
        });

        return {
          model: selectedModel.name,
          messages: visionMessages,
          max_tokens: visionMaxTokens,
          temperature: visionTemperature,
        } as ChatCompletionRequest;

      case 'embeddings':
        return {
          model: selectedModel.name,
          input: embeddingInput,
          encoding_format: encodingFormat,
        } as EmbeddingRequest;

      case 'images':
        return {
          model: selectedModel.name,
          prompt: imagePrompt,
          n: imageCount,
          size: imageSize,
          quality: imageQuality,
          style: imageStyle,
        } as ImageGenerationRequest;

      case 'audio-speech':
        return {
          model: selectedModel.name,
          input: speechInput,
          voice: speechVoice,
          response_format: speechFormat,
          speed: speechSpeed,
        } as AudioSpeechRequest;

      case 'audio-transcription':
        if (!recordedAudio) {
          throw new Error(
            'Please record or upload an audio file for transcription'
          );
        }

        return {
          model: selectedModel.name,
          file: recordedAudio,
          prompt: audioTranscriptionPrompt.trim() || undefined,
          response_format: audioResponseFormat,
          temperature: audioTemperature,
          language: audioLanguage.trim() || undefined,
        } as AudioTranscriptionRequest;

      case 'models':
        return null; // No request body needed for models endpoint

      default:
        return null;
    }
  };

  const testEndpointMutation = useMutation({
    mutationFn: async (requestData: EndpointRequestData) => {
      if (!selectedModel) {
        throw new Error('No model selected');
      }

      setError(null);
      setResponse(null);

      try {
        const response = await ModelService.testModel(
          selectedModel.id,
          selectedEndpoint,
          requestData
        );

        if (!response.success) {
          throw new Error(response.error || 'Test failed');
        }

        return response.data as ApiResponse;
      } catch (err: unknown) {
        const errorMessage =
          err instanceof Error
            ? err.message
            : 'Failed to test endpoint via proxy';
        throw new Error(errorMessage);
      }
    },
    onSuccess: (data) => {
      setResponse(data);
      toast.success(
        `${API_ENDPOINTS[selectedEndpoint].name} test completed successfully!`
      );
    },
    onError: (err: Error) => {
      const errorMessage = err?.message || 'Unknown error occurred';
      setError(errorMessage);
      toast.error(
        `${API_ENDPOINTS[selectedEndpoint].name} test failed: ${errorMessage}`
      );
    },
  });

  const handleTest = async () => {
    if (!selectedModel) {
      toast.error('Please select a model to test');
      return;
    }

    // Validate required fields based on endpoint
    if (selectedEndpoint === 'chat-completions' && !userMessage.trim()) {
      toast.error('Please enter a test message');
      return;
    }

    if (selectedEndpoint === 'vision-chat') {
      if (!visionUserMessage.trim()) {
        toast.error('Please enter a test message');
        return;
      }
      if (!selectedImage) {
        toast.error('Please select an image for vision analysis');
        return;
      }
    }

    if (selectedEndpoint === 'embeddings' && !embeddingInput.trim()) {
      toast.error('Please enter text for embedding');
      return;
    }

    if (selectedEndpoint === 'images' && !imagePrompt.trim()) {
      toast.error('Please enter an image prompt');
      return;
    }

    if (selectedEndpoint === 'audio-speech' && !speechInput.trim()) {
      toast.error('Please enter text for speech synthesis');
      return;
    }

    if (selectedEndpoint === 'audio-transcription' && !recordedAudio) {
      toast.error('Please record or upload an audio file for transcription');
      return;
    }

    const requestData = await buildRequest();
    testEndpointMutation.mutate(requestData);
  };

  const enabledModels = models.filter((model) => model.isEnabled);
  const credentials = selectedModel ? getModelCredentials(selectedModel) : null;
  const endpointUrl = credentials
    ? buildEndpointUrl(
        credentials.baseUrl,
        API_ENDPOINTS[selectedEndpoint].path
      )
    : '';

  return (
    <Card className='w-full'>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <Send className='h-5 w-5' />
          API Endpoint Tester
        </CardTitle>
        <CardDescription>
          Comprehensive testing of OpenAI-compatible API endpoints through the
          secure proxy (resolves CORS and network issues)
        </CardDescription>
      </CardHeader>
      <CardContent className='space-y-6'>
        {/* Model and Endpoint Selection */}
        <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
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
          </div>

          <div className='space-y-2'>
            <Label htmlFor='endpoint-select'>API Endpoint</Label>
            <Select
              value={selectedEndpoint}
              onValueChange={(value: EndpointType) =>
                setSelectedEndpoint(value)
              }
            >
              <SelectTrigger id='endpoint-select'>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(API_ENDPOINTS).map(([key, endpoint]) => {
                  const Icon = endpoint.icon;
                  return (
                    <SelectItem key={key} value={key}>
                      <div className='flex items-center gap-2'>
                        <Icon className='h-4 w-4' />
                        <span>{endpoint.name}</span>
                      </div>
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Model Information */}
        {selectedModel && credentials && (
          <div className='text-muted-foreground bg-muted space-y-2 rounded-md p-3 text-sm'>
            <div className='flex items-center gap-2'>
              <Globe className='h-4 w-4' />
              <span className='break-all'>
                <strong>Endpoint:</strong> {endpointUrl}
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
                <strong>Description:</strong>{' '}
                {API_ENDPOINTS[selectedEndpoint].description}
              </span>
            </div>
            {!credentials.apiKey && (
              <Alert variant='default' className='mt-2'>
                <Info className='h-4 w-4' />
                <AlertDescription>
                  No API key configured for this model. Testing may still work
                  if the model is free or if authentication is handled
                  elsewhere. For models requiring authentication, please add an
                  API key to the model or its provider group.
                </AlertDescription>
              </Alert>
            )}
          </div>
        )}

        <ApiEndpointForm
          selectedEndpoint={selectedEndpoint}
          maxTokens={maxTokens}
          setMaxTokens={setMaxTokens}
          temperature={temperature}
          setTemperature={setTemperature}
          systemMessage={systemMessage}
          setSystemMessage={setSystemMessage}
          userMessage={userMessage}
          setUserMessage={setUserMessage}
          visionMaxTokens={visionMaxTokens}
          setVisionMaxTokens={setVisionMaxTokens}
          visionTemperature={visionTemperature}
          setVisionTemperature={setVisionTemperature}
          visionSystemMessage={visionSystemMessage}
          setVisionSystemMessage={setVisionSystemMessage}
          visionUserMessage={visionUserMessage}
          setVisionUserMessage={setVisionUserMessage}
          imageDetail={imageDetail}
          setImageDetail={setImageDetail}
          selectedImage={selectedImage}
          imagePreviewUrl={imagePreviewUrl}
          onImageUpload={handleImageUpload}
          onRemoveImage={() => {
            setSelectedImage(null);
            setImagePreviewUrl(null);
          }}
          embeddingInput={embeddingInput}
          setEmbeddingInput={setEmbeddingInput}
          encodingFormat={encodingFormat}
          setEncodingFormat={setEncodingFormat}
          imageCount={imageCount}
          setImageCount={setImageCount}
          imageSize={imageSize}
          setImageSize={setImageSize}
          imageQuality={imageQuality}
          setImageQuality={setImageQuality}
          imageStyle={imageStyle}
          setImageStyle={setImageStyle}
          imagePrompt={imagePrompt}
          setImagePrompt={setImagePrompt}
          speechVoice={speechVoice}
          setSpeechVoice={setSpeechVoice}
          speechFormat={speechFormat}
          setSpeechFormat={setSpeechFormat}
          speechSpeed={speechSpeed}
          setSpeechSpeed={setSpeechSpeed}
          speechInput={speechInput}
          setSpeechInput={setSpeechInput}
          audioTranscriptionPrompt={audioTranscriptionPrompt}
          setAudioTranscriptionPrompt={setAudioTranscriptionPrompt}
          audioTemperature={audioTemperature}
          setAudioTemperature={setAudioTemperature}
          audioLanguage={audioLanguage}
          setAudioLanguage={setAudioLanguage}
          audioResponseFormat={audioResponseFormat}
          setAudioResponseFormat={setAudioResponseFormat}
          isRecording={isRecording}
          onStartRecording={startRecording}
          onStopRecording={stopRecording}
          onAudioUpload={handleAudioUpload}
          recordedAudio={recordedAudio}
          recordingUrl={recordingUrl}
          onRemoveAudio={() => {
            setRecordedAudio(null);
            setRecordingUrl(null);
          }}
        />

        {/* Test Button */}
        <Button
          onClick={handleTest}
          disabled={!selectedModelId || testEndpointMutation.isPending}
          className='w-full'
        >
          {testEndpointMutation.isPending ? (
            <>
              <Loader2 className='mr-2 h-4 w-4 animate-spin' />
              Testing {API_ENDPOINTS[selectedEndpoint].name}...
            </>
          ) : (
            <>
              <Send className='mr-2 h-4 w-4' />
              Test {API_ENDPOINTS[selectedEndpoint].name}
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
              <strong>Test Successful!</strong>{' '}
              {API_ENDPOINTS[selectedEndpoint].name} endpoint responded
              correctly.
            </AlertDescription>
          </Alert>
        )}

        <ApiEndpointResponse response={response} />

        {response && (
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
        )}
      </CardContent>
    </Card>
  );
}
