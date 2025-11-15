'use client';

import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
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
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Progress } from '@/components/ui/progress';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  ChevronLeft,
  Rocket,
  Server,
  Lock,
  Settings as SettingsIcon,
  Sparkles,
} from 'lucide-react';
import { AdminService, CreateUpstreamProvider, ProviderType } from '@/lib/api/services/admin';
import { apiClient } from '@/lib/api/client';
import { toast } from 'sonner';

interface OnboardingStep {
  id: string;
  title: string;
  description: string;
  icon: React.ReactNode;
}

const steps: OnboardingStep[] = [
  {
    id: 'welcome',
    title: 'Welcome to Routstr!',
    description: 'Let\'s get your AI routing system set up in just a few steps',
    icon: <Rocket className="h-12 w-12 text-blue-500" />,
  },
  {
    id: 'password',
    title: 'Set Admin Password',
    description: 'Secure your admin dashboard with a strong password',
    icon: <Lock className="h-12 w-12 text-green-500" />,
  },
  {
    id: 'provider',
    title: 'Add AI Provider',
    description: 'Connect your first AI provider to start routing requests',
    icon: <Server className="h-12 w-12 text-purple-500" />,
  },
  {
    id: 'settings',
    title: 'Optional Settings',
    description: 'Configure additional settings for your node',
    icon: <SettingsIcon className="h-12 w-12 text-orange-500" />,
  },
  {
    id: 'complete',
    title: 'All Set!',
    description: 'Your Routstr node is ready to route AI requests',
    icon: <Sparkles className="h-12 w-12 text-yellow-500" />,
  },
];

interface OnboardingWizardProps {
  open: boolean;
  onComplete: () => void;
}

export function OnboardingWizard({ open, onComplete }: OnboardingWizardProps) {
  const queryClient = useQueryClient();
  const [currentStep, setCurrentStep] = useState(0);
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordError, setPasswordError] = useState('');
  
  const [providerData, setProviderData] = useState<CreateUpstreamProvider>({
    provider_type: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1',
    api_key: '',
    api_version: null,
    enabled: true,
  });

  const [nodeName, setNodeName] = useState('');
  const [nodeDescription, setNodeDescription] = useState('');

  const { data: providerTypes = [] } = useQuery({
    queryKey: ['provider-types'],
    queryFn: () => AdminService.getProviderTypes(),
    enabled: open,
  });

  const setupPasswordMutation = useMutation({
    mutationFn: async (pwd: string) => {
      return await apiClient.post<{ ok: boolean; token?: string; expires_in?: number }>(
        '/admin/api/setup', 
        { password: pwd }
      );
    },
    onSuccess: async (data) => {
      if (data.token) {
        const expiresIn = data.expires_in || 3600;
        apiClient.setAuthToken(data.token, expiresIn);
      }
      
      try {
        const loginData = await AdminService.login(password);
        if (loginData.token) {
          apiClient.setAuthToken(loginData.token, loginData.expires_in);
        }
      } catch (error) {
        console.error('Login after setup failed:', error);
      }
      
      toast.success('Admin password set successfully');
      setCurrentStep((prev) => prev + 1);
    },
    onError: (error: Error) => {
      setPasswordError(error.message);
      toast.error(`Failed to set password: ${error.message}`);
    },
  });

  const createProviderMutation = useMutation({
    mutationFn: (data: CreateUpstreamProvider) =>
      AdminService.createUpstreamProvider(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      toast.success('Provider added successfully');
      setCurrentStep((prev) => prev + 1);
    },
    onError: (error: Error) => {
      toast.error(`Failed to add provider: ${error.message}`);
    },
  });

  const updateSettingsMutation = useMutation({
    mutationFn: async (settings: Record<string, unknown>) => {
      return await AdminService.updateSettings(settings);
    },
    onSuccess: () => {
      toast.success('Settings saved successfully');
      setCurrentStep((prev) => prev + 1);
    },
    onError: (error: Error) => {
      toast.error(`Failed to save settings: ${error.message}`);
    },
  });

  const handlePasswordSubmit = () => {
    setPasswordError('');
    
    if (password.length < 8) {
      setPasswordError('Password must be at least 8 characters');
      return;
    }
    
    if (password !== confirmPassword) {
      setPasswordError('Passwords do not match');
      return;
    }
    
    setupPasswordMutation.mutate(password);
  };

  const handleProviderSubmit = () => {
    if (!providerData.api_key.trim()) {
      toast.error('API key is required');
      return;
    }
    createProviderMutation.mutate(providerData);
  };

  const handleSettingsSubmit = () => {
    const settings: Record<string, unknown> = {};
    if (nodeName.trim()) {
      settings.name = nodeName;
    }
    if (nodeDescription.trim()) {
      settings.description = nodeDescription;
    }
    
    if (Object.keys(settings).length > 0) {
      updateSettingsMutation.mutate(settings);
    } else {
      setCurrentStep((prev) => prev + 1);
    }
  };

  const handleComplete = () => {
    localStorage.setItem('onboardingCompleted', 'true');
    onComplete();
  };

  const getDefaultBaseUrl = (type: string): string => {
    const providerType = providerTypes.find((pt: ProviderType) => pt.id === type);
    return providerType?.default_base_url || '';
  };

  const hasFixedBaseUrl = (type: string): boolean => {
    const providerType = providerTypes.find((pt: ProviderType) => pt.id === type);
    return providerType?.fixed_base_url || false;
  };

  const getPlatformUrl = (type: string): string | null => {
    const providerType = providerTypes.find((pt: ProviderType) => pt.id === type);
    return providerType?.platform_url || null;
  };

  const progress = ((currentStep + 1) / steps.length) * 100;

  const renderStepContent = () => {
    const step = steps[currentStep];

    switch (step.id) {
      case 'welcome':
        return (
          <div className="space-y-6 py-6">
            <div className="flex justify-center">{step.icon}</div>
            <div className="space-y-4 text-center">
              <h3 className="text-lg font-semibold">
                Welcome to Routstr - Your AI Request Router
              </h3>
              <p className="text-muted-foreground text-sm leading-relaxed">
                Routstr is a powerful payment-enabled proxy that routes AI requests to
                various providers. This wizard will help you:
              </p>
              <div className="space-y-3 text-left">
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="text-muted-foreground mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <p className="font-medium">Set up admin access</p>
                    <p className="text-muted-foreground text-sm">
                      Secure your dashboard with a password
                    </p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="text-muted-foreground mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <p className="font-medium">Connect AI providers</p>
                    <p className="text-muted-foreground text-sm">
                      Link OpenRouter, OpenAI, Anthropic, and more
                    </p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="text-muted-foreground mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <p className="font-medium">Configure your node</p>
                    <p className="text-muted-foreground text-sm">
                      Customize settings to match your needs
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        );

      case 'password':
        return (
          <div className="space-y-6 py-4">
            <div className="flex justify-center">{step.icon}</div>
            <div className="space-y-4">
              <div className="space-y-2">
                <h3 className="text-center text-lg font-semibold">
                  Create Admin Password
                </h3>
                <p className="text-muted-foreground text-center text-sm">
                  This password will be used to access the admin dashboard
                </p>
              </div>

              {passwordError && (
                <Alert variant="destructive">
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{passwordError}</AlertDescription>
                </Alert>
              )}

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="password">Password</Label>
                  <Input
                    id="password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="Enter password (min 8 characters)"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="confirmPassword">Confirm Password</Label>
                  <Input
                    id="confirmPassword"
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-enter password"
                  />
                </div>
              </div>

              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  Keep this password safe. You'll need it to access the admin panel.
                </AlertDescription>
              </Alert>
            </div>
          </div>
        );

      case 'provider':
        return (
          <div className="space-y-6 py-4">
            <div className="flex justify-center">{step.icon}</div>
            <div className="space-y-4">
              <div className="space-y-2">
                <h3 className="text-center text-lg font-semibold">
                  Connect Your First AI Provider
                </h3>
                <p className="text-muted-foreground text-center text-sm">
                  Add an API provider to start routing AI requests
                </p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="provider_type">Provider Type</Label>
                  <Select
                    value={providerData.provider_type}
                    onValueChange={(value) => {
                      setProviderData({
                        ...providerData,
                        provider_type: value,
                        base_url: getDefaultBaseUrl(value),
                      });
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {providerTypes.map((type) => (
                        <SelectItem key={type.id} value={type.id}>
                          {type.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="base_url">Base URL</Label>
                  <Input
                    id="base_url"
                    value={providerData.base_url}
                    onChange={(e) =>
                      setProviderData({ ...providerData, base_url: e.target.value })
                    }
                    placeholder="https://api.example.com/v1"
                    disabled={hasFixedBaseUrl(providerData.provider_type)}
                  />
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label htmlFor="api_key">API Key</Label>
                    {getPlatformUrl(providerData.provider_type) && (
                      <a
                        href={getPlatformUrl(providerData.provider_type)!}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                      >
                        Get Your API Key →
                      </a>
                    )}
                  </div>
                  <Input
                    id="api_key"
                    type="password"
                    value={providerData.api_key}
                    onChange={(e) =>
                      setProviderData({ ...providerData, api_key: e.target.value })
                    }
                    placeholder="sk-..."
                  />
                </div>

                {providerData.provider_type === 'azure' && (
                  <div className="space-y-2">
                    <Label htmlFor="api_version">API Version</Label>
                    <Input
                      id="api_version"
                      value={providerData.api_version || ''}
                      onChange={(e) =>
                        setProviderData({
                          ...providerData,
                          api_version: e.target.value || null,
                        })
                      }
                      placeholder="2024-02-15-preview"
                    />
                  </div>
                )}
              </div>

              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  Your API key is stored securely and only used to authenticate with the
                  provider. You can add more providers later from the Providers page.
                </AlertDescription>
              </Alert>
            </div>
          </div>
        );

      case 'settings':
        return (
          <div className="space-y-6 py-4">
            <div className="flex justify-center">{step.icon}</div>
            <div className="space-y-4">
              <div className="space-y-2">
                <h3 className="text-center text-lg font-semibold">
                  Customize Your Node (Optional)
                </h3>
                <p className="text-muted-foreground text-center text-sm">
                  Give your node a name and description - you can change these later
                </p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="nodeName">Node Name</Label>
                  <Input
                    id="nodeName"
                    value={nodeName}
                    onChange={(e) => setNodeName(e.target.value)}
                    placeholder="My Routstr Node"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="nodeDescription">Description</Label>
                  <Input
                    id="nodeDescription"
                    value={nodeDescription}
                    onChange={(e) => setNodeDescription(e.target.value)}
                    placeholder="AI request router with payment support"
                  />
                </div>
              </div>

              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  These settings are optional. You can configure more advanced options
                  like Nostr integration and Cashu mints later in the Settings page.
                </AlertDescription>
              </Alert>
            </div>
          </div>
        );

      case 'complete':
        return (
          <div className="space-y-6 py-6">
            <div className="flex justify-center">{step.icon}</div>
            <div className="space-y-4 text-center">
              <h3 className="text-lg font-semibold">You're All Set! 🎉</h3>
              <p className="text-muted-foreground text-sm leading-relaxed">
                Your Routstr node is configured and ready to route AI requests. Here's
                what you can do next:
              </p>
              <div className="space-y-3 text-left">
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">Add More Providers</CardTitle>
                    <CardDescription>
                      Visit the Providers page to add more AI providers
                    </CardDescription>
                  </CardHeader>
                </Card>
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">Configure Models</CardTitle>
                    <CardDescription>
                      Customize model pricing and settings in the Models page
                    </CardDescription>
                  </CardHeader>
                </Card>
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">Monitor Usage</CardTitle>
                    <CardDescription>
                      Track balances and transactions from the dashboard
                    </CardDescription>
                  </CardHeader>
                </Card>
              </div>
            </div>
          </div>
        );

      default:
        return null;
    }
  };

  const canProceed = () => {
    const step = steps[currentStep];
    
    switch (step.id) {
      case 'welcome':
        return true;
      case 'password':
        return password.length >= 8 && password === confirmPassword;
      case 'provider':
        return providerData.api_key.trim().length > 0;
      case 'settings':
        return true;
      case 'complete':
        return true;
      default:
        return false;
    }
  };

  const handleNext = () => {
    const step = steps[currentStep];
    
    switch (step.id) {
      case 'welcome':
        setCurrentStep((prev) => prev + 1);
        break;
      case 'password':
        handlePasswordSubmit();
        break;
      case 'provider':
        handleProviderSubmit();
        break;
      case 'settings':
        handleSettingsSubmit();
        break;
      case 'complete':
        handleComplete();
        break;
    }
  };

  const isLoading = 
    setupPasswordMutation.isPending ||
    createProviderMutation.isPending ||
    updateSettingsMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={() => {}}>
      <DialogContent className="max-w-2xl" onPointerDownOutside={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle className="text-2xl">{steps[currentStep].title}</DialogTitle>
          <DialogDescription>{steps[currentStep].description}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <Progress value={progress} className="h-2" />
          <div className="text-muted-foreground text-center text-sm">
            Step {currentStep + 1} of {steps.length}
          </div>
        </div>

        {renderStepContent()}

        <DialogFooter className="flex flex-col gap-2 sm:flex-row sm:justify-between">
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => setCurrentStep((prev) => Math.max(0, prev - 1))}
              disabled={currentStep === 0 || isLoading}
            >
              <ChevronLeft className="mr-2 h-4 w-4" />
              Back
            </Button>
            {currentStep === 0 && (
              <Button
                variant="ghost"
                onClick={handleComplete}
                disabled={isLoading}
              >
                Skip for now
              </Button>
            )}
          </div>
          <Button
            onClick={handleNext}
            disabled={!canProceed() || isLoading}
          >
            {isLoading ? (
              'Processing...'
            ) : currentStep === steps.length - 1 ? (
              'Get Started'
            ) : (
              <>
                Next
                <ChevronRight className="ml-2 h-4 w-4" />
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
