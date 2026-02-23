import type { ChangeEvent, Dispatch, SetStateAction } from 'react';
import Image from 'next/image';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { Image as ImageIcon, List, Mic, MicOff, Volume2 } from 'lucide-react';
import type { EndpointType } from '@/components/api-endpoint-types';

interface ApiEndpointFormProps {
  selectedEndpoint: EndpointType;
  maxTokens: number;
  setMaxTokens: Dispatch<SetStateAction<number>>;
  temperature: number;
  setTemperature: Dispatch<SetStateAction<number>>;
  systemMessage: string;
  setSystemMessage: Dispatch<SetStateAction<string>>;
  userMessage: string;
  setUserMessage: Dispatch<SetStateAction<string>>;

  visionMaxTokens: number;
  setVisionMaxTokens: Dispatch<SetStateAction<number>>;
  visionTemperature: number;
  setVisionTemperature: Dispatch<SetStateAction<number>>;
  visionSystemMessage: string;
  setVisionSystemMessage: Dispatch<SetStateAction<string>>;
  visionUserMessage: string;
  setVisionUserMessage: Dispatch<SetStateAction<string>>;
  imageDetail: 'low' | 'high' | 'auto';
  setImageDetail: Dispatch<SetStateAction<'low' | 'high' | 'auto'>>;
  selectedImage: File | null;
  imagePreviewUrl: string | null;
  onImageUpload: (event: ChangeEvent<HTMLInputElement>) => void;
  onRemoveImage: () => void;

  embeddingInput: string;
  setEmbeddingInput: Dispatch<SetStateAction<string>>;
  encodingFormat: 'float' | 'base64';
  setEncodingFormat: Dispatch<SetStateAction<'float' | 'base64'>>;

  imageCount: number;
  setImageCount: Dispatch<SetStateAction<number>>;
  imageSize: '256x256' | '512x512' | '1024x1024' | '1792x1024' | '1024x1792';
  setImageSize: Dispatch<
    SetStateAction<'256x256' | '512x512' | '1024x1024' | '1792x1024' | '1024x1792'>
  >;
  imageQuality: 'standard' | 'hd';
  setImageQuality: Dispatch<SetStateAction<'standard' | 'hd'>>;
  imageStyle: 'vivid' | 'natural';
  setImageStyle: Dispatch<SetStateAction<'vivid' | 'natural'>>;
  imagePrompt: string;
  setImagePrompt: Dispatch<SetStateAction<string>>;

  speechVoice: 'alloy' | 'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer';
  setSpeechVoice: Dispatch<
    SetStateAction<'alloy' | 'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer'>
  >;
  speechFormat: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm';
  setSpeechFormat: Dispatch<
    SetStateAction<'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm'>
  >;
  speechSpeed: number;
  setSpeechSpeed: Dispatch<SetStateAction<number>>;
  speechInput: string;
  setSpeechInput: Dispatch<SetStateAction<string>>;

  audioTranscriptionPrompt: string;
  setAudioTranscriptionPrompt: Dispatch<SetStateAction<string>>;
  audioTemperature: number;
  setAudioTemperature: Dispatch<SetStateAction<number>>;
  audioLanguage: string;
  setAudioLanguage: Dispatch<SetStateAction<string>>;
  audioResponseFormat: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  setAudioResponseFormat: Dispatch<
    SetStateAction<'json' | 'text' | 'srt' | 'verbose_json' | 'vtt'>
  >;
  isRecording: boolean;
  onStartRecording: () => void;
  onStopRecording: () => void;
  onAudioUpload: (event: ChangeEvent<HTMLInputElement>) => void;
  recordedAudio: File | null;
  recordingUrl: string | null;
  onRemoveAudio: () => void;
}

export function ApiEndpointForm({
  selectedEndpoint,
  maxTokens,
  setMaxTokens,
  temperature,
  setTemperature,
  systemMessage,
  setSystemMessage,
  userMessage,
  setUserMessage,
  visionMaxTokens,
  setVisionMaxTokens,
  visionTemperature,
  setVisionTemperature,
  visionSystemMessage,
  setVisionSystemMessage,
  visionUserMessage,
  setVisionUserMessage,
  imageDetail,
  setImageDetail,
  selectedImage,
  imagePreviewUrl,
  onImageUpload,
  onRemoveImage,
  embeddingInput,
  setEmbeddingInput,
  encodingFormat,
  setEncodingFormat,
  imageCount,
  setImageCount,
  imageSize,
  setImageSize,
  imageQuality,
  setImageQuality,
  imageStyle,
  setImageStyle,
  imagePrompt,
  setImagePrompt,
  speechVoice,
  setSpeechVoice,
  speechFormat,
  setSpeechFormat,
  speechSpeed,
  setSpeechSpeed,
  speechInput,
  setSpeechInput,
  audioTranscriptionPrompt,
  setAudioTranscriptionPrompt,
  audioTemperature,
  setAudioTemperature,
  audioLanguage,
  setAudioLanguage,
  audioResponseFormat,
  setAudioResponseFormat,
  isRecording,
  onStartRecording,
  onStopRecording,
  onAudioUpload,
  recordedAudio,
  recordingUrl,
  onRemoveAudio,
}: ApiEndpointFormProps) {
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
                onChange={(event) => setMaxTokens(parseInt(event.target.value) || 150)}
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
                onChange={(event) =>
                  setTemperature(parseFloat(event.target.value) || 0.7)
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
              onChange={(event) => setSystemMessage(event.target.value)}
              rows={2}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='user-message'>Test Message</Label>
            <Textarea
              id='user-message'
              placeholder='Enter your test message...'
              value={userMessage}
              onChange={(event) => setUserMessage(event.target.value)}
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
                onChange={(event) =>
                  setVisionMaxTokens(parseInt(event.target.value) || 300)
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
                onChange={(event) =>
                  setVisionTemperature(parseFloat(event.target.value) || 0.7)
                }
              />
            </div>
          </div>

          <div className='space-y-2'>
            <Label htmlFor='vision-system-message'>System Message (Optional)</Label>
            <Textarea
              id='vision-system-message'
              placeholder='Enter system message...'
              value={visionSystemMessage}
              onChange={(event) => setVisionSystemMessage(event.target.value)}
              rows={2}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='vision-user-message'>Test Message</Label>
            <Textarea
              id='vision-user-message'
              placeholder='Enter your test message...'
              value={visionUserMessage}
              onChange={(event) => setVisionUserMessage(event.target.value)}
              rows={3}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='image-detail'>Image Detail</Label>
            <Select
              value={imageDetail}
              onValueChange={(value: 'low' | 'high' | 'auto') => setImageDetail(value)}
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
            <Input type='file' accept='image/*' onChange={onImageUpload} className='cursor-pointer' />
            {selectedImage && (
              <div className='mt-2 flex flex-wrap items-center gap-2'>
                <ImageIcon className='text-muted-foreground h-5 w-5' />
                <span className='text-muted-foreground text-sm'>
                  Selected image: {selectedImage.name}
                </span>
                <Button
                  variant='outline'
                  size='sm'
                  onClick={onRemoveImage}
                  className='ml-0 sm:ml-auto'
                >
                  Remove
                </Button>
              </div>
            )}
            {imagePreviewUrl && (
              <div className='relative mt-2 aspect-square w-32 overflow-hidden rounded-md border'>
                <Image src={imagePreviewUrl} alt='Image preview' fill className='object-cover' />
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
              onValueChange={(value: 'float' | 'base64') => setEncodingFormat(value)}
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
              onChange={(event) => setEmbeddingInput(event.target.value)}
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
                onChange={(event) => setImageCount(parseInt(event.target.value) || 1)}
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
                onValueChange={(value: 'standard' | 'hd') => setImageQuality(value)}
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
              onValueChange={(value: 'vivid' | 'natural') => setImageStyle(value)}
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
              onChange={(event) => setImagePrompt(event.target.value)}
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
                  value: 'alloy' | 'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer'
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
                onChange={(event) => setSpeechSpeed(parseFloat(event.target.value) || 1.0)}
              />
            </div>
          </div>

          <div className='space-y-2'>
            <Label htmlFor='speech-input'>Text to Synthesize</Label>
            <Textarea
              id='speech-input'
              placeholder='Enter text to convert to speech...'
              value={speechInput}
              onChange={(event) => setSpeechInput(event.target.value)}
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
              <Label htmlFor='audio-transcription-prompt'>Prompt (Optional)</Label>
              <Textarea
                id='audio-transcription-prompt'
                placeholder='Enter a prompt for the transcription...'
                value={audioTranscriptionPrompt}
                onChange={(event) => setAudioTranscriptionPrompt(event.target.value)}
                rows={2}
              />
            </div>
            <div className='space-y-2'>
              <Label htmlFor='audio-transcription-temperature'>Temperature</Label>
              <Input
                id='audio-transcription-temperature'
                type='number'
                min={0}
                max={1}
                step={0.1}
                value={audioTemperature}
                onChange={(event) =>
                  setAudioTemperature(parseFloat(event.target.value) || 0.0)
                }
              />
            </div>
          </div>

          <div className='space-y-2'>
            <Label htmlFor='audio-transcription-language'>Language (Optional)</Label>
            <Input
              id='audio-transcription-language'
              placeholder='e.g., en-US, fr-FR'
              value={audioLanguage}
              onChange={(event) => setAudioLanguage(event.target.value)}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='audio-transcription-response-format'>Response Format</Label>
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
              <div className='flex flex-wrap items-center gap-2'>
                <Button
                  type='button'
                  variant={isRecording ? 'destructive' : 'default'}
                  size='sm'
                  onClick={isRecording ? onStopRecording : onStartRecording}
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
                  <Badge variant='destructive' className='gap-1.5'>
                    <span className='h-2 w-2 animate-pulse rounded-full bg-current' />
                    Recording...
                  </Badge>
                )}
              </div>
            </div>

            <div className='space-y-2'>
              <Label htmlFor='audio-transcription-upload'>Or Upload Audio File</Label>
              <Input type='file' accept='audio/*' onChange={onAudioUpload} className='cursor-pointer' />
              {recordedAudio && (
                <div className='mt-2 flex flex-wrap items-center gap-2'>
                  <Volume2 className='text-muted-foreground h-5 w-5' />
                  <span className='text-muted-foreground text-sm'>
                    Selected audio: {recordedAudio.name}
                  </span>
                  <Button
                    variant='outline'
                    size='sm'
                    onClick={onRemoveAudio}
                    className='ml-0 sm:ml-auto'
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
}
