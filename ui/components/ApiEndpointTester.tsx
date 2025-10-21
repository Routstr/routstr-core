'use client';

import React, { useState, useRef } from 'react';
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
  FileText,
  Image as ImageIcon,
  Mic,
  List,
  MicOff,
  Eye,
  Volume2,
} from 'lucide-react';
import { toast } from 'sonner';
import { Input } from '@/components/ui/input';
import Image from 'next/image';

interface ApiEndpointTesterProps {
  models: Model[];
}

// API Endpoint Types
const API_ENDPOINTS = {
  'chat-completions': {
    name: 'Chat Completions',
    path: '/chat/completions',
    icon: FileText,
    description: 'Test conversational AI with chat completion requests',
  },
  'vision-chat': {
    name: 'Vision Chat (Image + Text)',
    path: '/chat/completions',
    icon: Eye,
    description: 'Analyze images with text prompts using vision models',
  },
  embeddings: {
    name: 'Embeddings',
    path: '/embeddings',
    icon: List,
    description: 'Generate embeddings for text input',
  },
  images: {
    name: 'Image Generation',
    path: '/images/generations',
    icon: ImageIcon,
    description: 'Generate images from text prompts',
  },
  'audio-speech': {
    name: 'Text-to-Speech',
    path: '/audio/speech',
    icon: Mic,
    description: 'Convert text to speech audio',
  },
  'audio-transcription': {
    name: 'Audio Transcription',
    path: '/audio/transcriptions',
    icon: Volume2,
    description: 'Transcribe audio files to text',
  },
  models: {
    name: 'List Models',
    path: '/models',
    icon: List,
    description: 'List all available models from the provider',
  },
} as const;

type EndpointType = keyof typeof API_ENDPOINTS;

// Request/Response interfaces for different endpoints
interface ChatCompletionRequest {
  model: string;
  messages: {
    role: 'system' | 'user' | 'assistant';
    content:
      | string
      | Array<{
          type: 'text' | 'image_url';
          text?: string;
          image_url?: {
            url: string;
            detail?: 'low' | 'high' | 'auto';
          };
        }>;
  }[];
  max_tokens?: number;
  temperature?: number;
}

interface EmbeddingRequest {
  model: string;
  input: string | string[];
  encoding_format?: 'float' | 'base64';
}

interface ImageGenerationRequest {
  model?: string;
  prompt: string;
  n?: number;
  size?: '256x256' | '512x512' | '1024x1024' | '1792x1024' | '1024x1792';
  quality?: 'standard' | 'hd';
  style?: 'vivid' | 'natural';
}

interface AudioSpeechRequest {
  model: string;
  input: string;
  voice: 'alloy' | 'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer';
  response_format?: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm';
  speed?: number;
}

interface AudioTranscriptionRequest {
  model: string;
  file: File;
  prompt?: string;
  response_format?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  temperature?: number;
  language?: string;
}

// Response types
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

interface EmbeddingResponse {
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

interface ImageGenerationResponse {
  created: number;
  data: {
    url: string;
    revised_prompt?: string;
  }[];
}

interface AudioResponse {
  type: 'audio';
  url: string;
  size: number;
}

interface AudioTranscriptionResponse {
  text: string;
}

interface ModelsListResponse {
  object: string;
  data: {
    id: string;
    object?: string;
    created?: number;
  }[];
}

type ApiResponse =
  | ChatCompletionResponse
  | EmbeddingResponse
  | ImageGenerationResponse
  | AudioResponse
  | AudioTranscriptionResponse
  | ModelsListResponse;

const DEFAULT_REQUESTS = {
  'chat-completions': {
    systemMessage: 'You are a helpful assistant. Please respond concisely.',
    userMessage:
      'Hello! Can you tell me what model you are and confirm that you are working correctly?',
    maxTokens: 150,
    temperature: 0.7,
  },
  'vision-chat': {
    systemMessage:
      'You are a helpful assistant that can analyze images. Please describe what you see.',
    userMessage:
      'What do you see in this image? Please provide a detailed description.',
    maxTokens: 300,
    temperature: 0.7,
    imageDetail: 'auto' as const,
  },
  embeddings: {
    input: 'The quick brown fox jumps over the lazy dog.',
    encoding_format: 'float' as const,
  },
  images: {
    prompt: 'A beautiful sunset over a mountain landscape',
    n: 1,
    size: '1024x1024' as const,
    quality: 'standard' as const,
    style: 'vivid' as const,
  },
  'audio-speech': {
    input: 'Hello, this is a test of the text-to-speech functionality.',
    voice: 'alloy' as const,
    response_format: 'mp3' as const,
    speed: 1.0,
  },
  'audio-transcription': {
    prompt: 'This is a test transcription.',
    response_format: 'json' as const,
    temperature: 0.0,
    language: '',
  },
};

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

  // File upload refs
  const imageInputRef = useRef<HTMLInputElement>(null);
  const audioInputRef = useRef<HTMLInputElement>(null);

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
    } catch (error) {
      console.error('Error starting recording:', error);
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

  const buildRequest = async (): Promise<
    | ChatCompletionRequest
    | EmbeddingRequest
    | ImageGenerationRequest
    | AudioSpeechRequest
    | AudioTranscriptionRequest
    | null
  > => {
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
    mutationFn: async (
      requestData:
        | ChatCompletionRequest
        | EmbeddingRequest
        | ImageGenerationRequest
        | AudioSpeechRequest
        | AudioTranscriptionRequest
        | null
    ) => {
      if (!selectedModel) {
        throw new Error('No model selected');
      }

      setError(null);
      setResponse(null);

      try {
        console.log(`Testing endpoint via proxy: ${selectedEndpoint}`);
        console.log('Request payload:', requestData);

        const response = await ModelService.testModel(
          selectedModel.id,
          selectedEndpoint,
          requestData as unknown as Record<string, unknown>
        );

        if (!response.success) {
          throw new Error(response.error || 'Test failed');
        }

        return response.data as ApiResponse;
      } catch (err: unknown) {
        console.error('API endpoint test error via proxy:', err);

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

  const renderEndpointForm = () => {
    switch (selectedEndpoint) {
      case 'chat-completions':
        return (
          <div className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
              <div className='space-y-2'>
                <Label htmlFor='max-tokens'>Max Tokens</Label>
                <Input
                  id='max-tokens'
                  type='number'
                  min={1}
                  max={4000}
                  value={maxTokens}
                  onChange={(e) =>
                    setMaxTokens(parseInt(e.target.value) || 150)
                  }
                />
              </div>
              <div className='space-y-2'>
                <Label htmlFor='temperature'>Temperature</Label>
                <Input
                  id='temperature'
                  type='number'
                  min={0}
                  max={2}
                  step={0.1}
                  value={temperature}
                  onChange={(e) =>
                    setTemperature(parseFloat(e.target.value) || 0.7)
                  }
                />
              </div>
            </div>

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
          </div>
        );

      case 'vision-chat':
        return (
          <div className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
              <div className='space-y-2'>
                <Label htmlFor='vision-max-tokens'>Max Tokens</Label>
                <Input
                  id='vision-max-tokens'
                  type='number'
                  min={1}
                  max={4000}
                  value={visionMaxTokens}
                  onChange={(e) =>
                    setVisionMaxTokens(parseInt(e.target.value) || 300)
                  }
                />
              </div>
              <div className='space-y-2'>
                <Label htmlFor='vision-temperature'>Temperature</Label>
                <Input
                  id='vision-temperature'
                  type='number'
                  min={0}
                  max={2}
                  step={0.1}
                  value={visionTemperature}
                  onChange={(e) =>
                    setVisionTemperature(parseFloat(e.target.value) || 0.7)
                  }
                />
              </div>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='vision-system-message'>
                System Message (Optional)
              </Label>
              <Textarea
                id='vision-system-message'
                placeholder='Enter system message...'
                value={visionSystemMessage}
                onChange={(e) => setVisionSystemMessage(e.target.value)}
                rows={2}
              />
            </div>

            <div className='space-y-2'>
              <Label htmlFor='vision-user-message'>Test Message</Label>
              <Textarea
                id='vision-user-message'
                placeholder='Enter your test message...'
                value={visionUserMessage}
                onChange={(e) => setVisionUserMessage(e.target.value)}
                rows={3}
              />
            </div>

            <div className='space-y-2'>
              <Label htmlFor='image-detail'>Image Detail</Label>
              <Select
                value={imageDetail}
                onValueChange={(value: 'low' | 'high' | 'auto') =>
                  setImageDetail(value)
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='low'>Low</SelectItem>
                  <SelectItem value='high'>High</SelectItem>
                  <SelectItem value='auto'>Auto</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='image-upload'>Upload Image (Optional)</Label>
              <Input
                type='file'
                accept='image/*'
                onChange={handleImageUpload}
                ref={imageInputRef}
                className='file:mr-4 file:rounded-full file:border-0 file:bg-blue-50 file:px-4 file:py-2 file:text-sm file:font-semibold file:text-blue-700 hover:file:bg-blue-100'
              />
              {selectedImage && (
                <div className='mt-2 flex items-center gap-2'>
                  <ImageIcon className='h-5 w-5 text-blue-500' />
                  <span className='text-muted-foreground text-sm'>
                    Selected image: {selectedImage.name}
                  </span>
                  <Button
                    variant='outline'
                    size='sm'
                    onClick={() => setSelectedImage(null)}
                    className='ml-auto'
                  >
                    Remove
                  </Button>
                </div>
              )}
              {imagePreviewUrl && (
                <div className='relative mt-2 aspect-square w-32 overflow-hidden rounded-md border'>
                  <Image
                    src={imagePreviewUrl}
                    alt='Image preview'
                    fill
                    className='object-cover'
                  />
                </div>
              )}
            </div>
          </div>
        );

      case 'embeddings':
        return (
          <div className='space-y-4'>
            <div className='space-y-2'>
              <Label htmlFor='encoding-format'>Encoding Format</Label>
              <Select
                value={encodingFormat}
                onValueChange={(value: 'float' | 'base64') =>
                  setEncodingFormat(value)
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='float'>Float</SelectItem>
                  <SelectItem value='base64'>Base64</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='embedding-input'>Text Input</Label>
              <Textarea
                id='embedding-input'
                placeholder='Enter text to generate embeddings for...'
                value={embeddingInput}
                onChange={(e) => setEmbeddingInput(e.target.value)}
                rows={4}
              />
            </div>
          </div>
        );

      case 'images':
        return (
          <div className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3'>
              <div className='space-y-2'>
                <Label htmlFor='image-count'>Number of Images</Label>
                <Input
                  id='image-count'
                  type='number'
                  min={1}
                  max={10}
                  value={imageCount}
                  onChange={(e) => setImageCount(parseInt(e.target.value) || 1)}
                />
              </div>

              <div className='space-y-2'>
                <Label htmlFor='image-size'>Size</Label>
                <Select
                  value={imageSize}
                  onValueChange={(
                    value:
                      | '256x256'
                      | '512x512'
                      | '1024x1024'
                      | '1792x1024'
                      | '1024x1792'
                  ) => setImageSize(value)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value='256x256'>256x256</SelectItem>
                    <SelectItem value='512x512'>512x512</SelectItem>
                    <SelectItem value='1024x1024'>1024x1024</SelectItem>
                    <SelectItem value='1792x1024'>1792x1024</SelectItem>
                    <SelectItem value='1024x1792'>1024x1792</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className='space-y-2'>
                <Label htmlFor='image-quality'>Quality</Label>
                <Select
                  value={imageQuality}
                  onValueChange={(value: 'standard' | 'hd') =>
                    setImageQuality(value)
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value='standard'>Standard</SelectItem>
                    <SelectItem value='hd'>HD</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='image-style'>Style</Label>
              <Select
                value={imageStyle}
                onValueChange={(value: 'vivid' | 'natural') =>
                  setImageStyle(value)
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='vivid'>Vivid</SelectItem>
                  <SelectItem value='natural'>Natural</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='image-prompt'>Image Prompt</Label>
              <Textarea
                id='image-prompt'
                placeholder='Describe the image you want to generate...'
                value={imagePrompt}
                onChange={(e) => setImagePrompt(e.target.value)}
                rows={3}
              />
            </div>
          </div>
        );

      case 'audio-speech':
        return (
          <div className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 md:grid-cols-3'>
              <div className='space-y-2'>
                <Label htmlFor='speech-voice'>Voice</Label>
                <Select
                  value={speechVoice}
                  onValueChange={(
                    value:
                      | 'alloy'
                      | 'echo'
                      | 'fable'
                      | 'onyx'
                      | 'nova'
                      | 'shimmer'
                  ) => setSpeechVoice(value)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value='alloy'>Alloy</SelectItem>
                    <SelectItem value='echo'>Echo</SelectItem>
                    <SelectItem value='fable'>Fable</SelectItem>
                    <SelectItem value='onyx'>Onyx</SelectItem>
                    <SelectItem value='nova'>Nova</SelectItem>
                    <SelectItem value='shimmer'>Shimmer</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className='space-y-2'>
                <Label htmlFor='speech-format'>Response Format</Label>
                <Select
                  value={speechFormat}
                  onValueChange={(
                    value: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm'
                  ) => setSpeechFormat(value)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value='mp3'>MP3</SelectItem>
                    <SelectItem value='opus'>Opus</SelectItem>
                    <SelectItem value='aac'>AAC</SelectItem>
                    <SelectItem value='flac'>FLAC</SelectItem>
                    <SelectItem value='wav'>WAV</SelectItem>
                    <SelectItem value='pcm'>PCM</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className='space-y-2'>
                <Label htmlFor='speech-speed'>Speed</Label>
                <Input
                  id='speech-speed'
                  type='number'
                  min={0.25}
                  max={4.0}
                  step={0.25}
                  value={speechSpeed}
                  onChange={(e) =>
                    setSpeechSpeed(parseFloat(e.target.value) || 1.0)
                  }
                />
              </div>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='speech-input'>Text to Synthesize</Label>
              <Textarea
                id='speech-input'
                placeholder='Enter text to convert to speech...'
                value={speechInput}
                onChange={(e) => setSpeechInput(e.target.value)}
                rows={4}
              />
            </div>
          </div>
        );

      case 'audio-transcription':
        return (
          <div className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
              <div className='space-y-2'>
                <Label htmlFor='audio-transcription-prompt'>
                  Prompt (Optional)
                </Label>
                <Textarea
                  id='audio-transcription-prompt'
                  placeholder='Enter a prompt for the transcription...'
                  value={audioTranscriptionPrompt}
                  onChange={(e) => setAudioTranscriptionPrompt(e.target.value)}
                  rows={2}
                />
              </div>
              <div className='space-y-2'>
                <Label htmlFor='audio-transcription-temperature'>
                  Temperature
                </Label>
                <Input
                  id='audio-transcription-temperature'
                  type='number'
                  min={0}
                  max={1}
                  step={0.1}
                  value={audioTemperature}
                  onChange={(e) =>
                    setAudioTemperature(parseFloat(e.target.value) || 0.0)
                  }
                />
              </div>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='audio-transcription-language'>
                Language (Optional)
              </Label>
              <Input
                id='audio-transcription-language'
                placeholder='e.g., en-US, fr-FR'
                value={audioLanguage}
                onChange={(e) => setAudioLanguage(e.target.value)}
              />
            </div>

            <div className='space-y-2'>
              <Label htmlFor='audio-transcription-response-format'>
                Response Format
              </Label>
              <Select
                value={audioResponseFormat}
                onValueChange={(
                  value: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt'
                ) => setAudioResponseFormat(value)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='json'>JSON</SelectItem>
                  <SelectItem value='text'>Text</SelectItem>
                  <SelectItem value='srt'>SRT</SelectItem>
                  <SelectItem value='verbose_json'>Verbose JSON</SelectItem>
                  <SelectItem value='vtt'>VTT</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className='space-y-4'>
              <div className='space-y-2'>
                <Label>Voice Recording</Label>
                <div className='flex items-center gap-2'>
                  <Button
                    type='button'
                    variant={isRecording ? 'destructive' : 'default'}
                    size='sm'
                    onClick={isRecording ? stopRecording : startRecording}
                    className='flex items-center gap-2'
                  >
                    {isRecording ? (
                      <>
                        <MicOff className='h-4 w-4' />
                        Stop Recording
                      </>
                    ) : (
                      <>
                        <Mic className='h-4 w-4' />
                        Start Recording
                      </>
                    )}
                  </Button>
                  {isRecording && (
                    <div className='flex items-center gap-2 text-red-600'>
                      <div className='h-2 w-2 animate-pulse rounded-full bg-red-600' />
                      <span className='text-sm'>Recording...</span>
                    </div>
                  )}
                </div>
              </div>

              <div className='space-y-2'>
                <Label htmlFor='audio-transcription-upload'>
                  Or Upload Audio File
                </Label>
                <Input
                  type='file'
                  accept='audio/*'
                  onChange={handleAudioUpload}
                  ref={audioInputRef}
                  className='file:mr-4 file:rounded-full file:border-0 file:bg-blue-50 file:px-4 file:py-2 file:text-sm file:font-semibold file:text-blue-700 hover:file:bg-blue-100'
                />
                {recordedAudio && (
                  <div className='mt-2 flex items-center gap-2'>
                    <Volume2 className='h-5 w-5 text-blue-500' />
                    <span className='text-muted-foreground text-sm'>
                      Selected audio: {recordedAudio.name}
                    </span>
                    <Button
                      variant='outline'
                      size='sm'
                      onClick={() => {
                        setRecordedAudio(null);
                        setRecordingUrl(null);
                      }}
                      className='ml-auto'
                    >
                      Remove
                    </Button>
                  </div>
                )}
                {recordingUrl && (
                  <div className='bg-muted mt-2 rounded-md p-4'>
                    <audio controls src={recordingUrl} className='w-full'>
                      Your browser does not support the audio element.
                    </audio>
                  </div>
                )}
              </div>
            </div>
          </div>
        );

      case 'models':
        return (
          <div className='space-y-4'>
            <Alert>
              <List className='h-4 w-4' />
              <AlertDescription>
                This endpoint lists all available models from the provider. No
                additional parameters are required.
              </AlertDescription>
            </Alert>
          </div>
        );

      default:
        return null;
    }
  };

  // Type guard functions
  const isAudioResponse = (
    response: ApiResponse
  ): response is AudioResponse => {
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

  const renderResponse = () => {
    if (!response) return null;

    // Handle audio response
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

    // Handle audio transcription response
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

    // Handle different response types using type guards
    if (isChatCompletionResponse(response)) {
      return (
        <div className='space-y-4'>
          <div className='space-y-2'>
            <Label>Model Response</Label>
            <div className='bg-muted rounded-md p-4'>
              <p className='text-sm whitespace-pre-wrap'>
                {response.choices?.[0]?.message?.content ||
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
                  <div className='text-muted-foreground'>Completion Tokens</div>
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
                Generated {response.data?.[0]?.embedding?.length || 0}{' '}
                dimensional embedding vector
              </p>
              <details className='group'>
                <summary className='hover:text-foreground cursor-pointer text-sm'>
                  Show first 10 values
                </summary>
                <pre className='mt-2 text-xs'>
                  {JSON.stringify(
                    response.data?.[0]?.embedding?.slice(0, 10),
                    null,
                    2
                  )}
                  ...
                </pre>
              </details>
            </div>
          </div>

          {response.usage && (
            <div className='space-y-2'>
              <Label>Usage Statistics</Label>
              <div className='bg-muted rounded p-2 text-center text-sm'>
                <div className='font-semibold'>
                  {response.usage.total_tokens}
                </div>
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
                        Created:{' '}
                        {new Date(model.created * 1000).toLocaleDateString()}
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
  };

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
              <span>
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

        {/* Endpoint-specific form */}
        {renderEndpointForm()}

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

        {response && renderResponse()}

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
