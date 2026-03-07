import {
  FileText,
  Eye,
  List,
  Image as ImageIcon,
  Mic,
  Volume2,
} from 'lucide-react';

export const API_ENDPOINTS = {
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

export type EndpointType = keyof typeof API_ENDPOINTS;

export interface ChatCompletionRequest {
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

export interface EmbeddingRequest {
  model: string;
  input: string | string[];
  encoding_format?: 'float' | 'base64';
}

export interface ImageGenerationRequest {
  model?: string;
  prompt: string;
  n?: number;
  size?: '256x256' | '512x512' | '1024x1024' | '1792x1024' | '1024x1792';
  quality?: 'standard' | 'hd';
  style?: 'vivid' | 'natural';
}

export interface AudioSpeechRequest {
  model: string;
  input: string;
  voice: 'alloy' | 'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer';
  response_format?: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm';
  speed?: number;
}

export interface AudioTranscriptionRequest {
  model: string;
  file: File;
  prompt?: string;
  response_format?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  temperature?: number;
  language?: string;
}

export const DEFAULT_REQUESTS = {
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

export type EndpointRequestData =
  | ChatCompletionRequest
  | EmbeddingRequest
  | ImageGenerationRequest
  | AudioSpeechRequest
  | AudioTranscriptionRequest
  | null;
