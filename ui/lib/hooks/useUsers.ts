import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { UserService } from '../api/services/users';
import { CreateUser, UpdateUser } from '../api/schemas/users';
import { toast } from 'sonner';

// Query keys for users
const userKeys = {
  all: ['users'] as const,
  lists: () => [...userKeys.all, 'list'] as const,
  list: (filters: { organizationId?: string }) =>
    [...userKeys.lists(), filters] as const,
  details: () => [...userKeys.all, 'detail'] as const,
  detail: (id: string) => [...userKeys.details(), id] as const,
};

/**
 * Hook to fetch users list with optional organization filter
 */
export function useUsers(filters?: {
  organizationId?: string;
  enabled?: boolean;
}) {
  const { organizationId, enabled } = filters || {};

  return useQuery({
    queryKey: userKeys.list({ organizationId }),
    queryFn: () => UserService.listUsers(organizationId),
    enabled: enabled ?? true,
  });
}

/**
 * Hook to fetch a specific user by ID
 */
export function useUser(userId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: userKeys.detail(userId),
    queryFn: () => UserService.getUser(userId),
    enabled: !!userId && (options?.enabled ?? true),
  });
}

/**
 * Hook to create a new user
 */
export function useCreateUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (userData: CreateUser) => {
      return UserService.createUser(userData);
    },
    onSuccess: (user) => {
      toast.success('User created successfully');
      queryClient.invalidateQueries({ queryKey: userKeys.lists() });
      queryClient.invalidateQueries({
        queryKey: userKeys.list({ organizationId: user.organization_id }),
      });
    },
    onError: (error) => {
      toast.error(`Failed to create user: ${error.message}`);
    },
  });
}

/**
 * Hook to update an existing user
 */
export function useUpdateUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      userId,
      userData,
    }: {
      userId: string;
      userData: UpdateUser;
    }) => {
      return UserService.updateUser(userId, userData);
    },
    onSuccess: (updatedUser) => {
      if (updatedUser) {
        toast.success('User updated successfully');
        queryClient.invalidateQueries({
          queryKey: userKeys.detail(updatedUser.id),
        });
        queryClient.invalidateQueries({ queryKey: userKeys.lists() });
        queryClient.invalidateQueries({
          queryKey: userKeys.list({
            organizationId: updatedUser.organization_id,
          }),
        });
      }
    },
    onError: (error) => {
      toast.error(`Failed to update user: ${error.message}`);
    },
  });
}

/**
 * Hook to delete a user
 */
export function useDeleteUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (userId: string) => {
      return UserService.deleteUser(userId);
    },
    onSuccess: () => {
      toast.success('User deleted successfully');
      queryClient.invalidateQueries({ queryKey: userKeys.lists() });
    },
    onError: (error) => {
      toast.error(`Failed to delete user: ${error.message}`);
    },
  });
}
