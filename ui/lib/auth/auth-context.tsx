import React, { createContext, useContext, useState } from 'react';
import { User } from '@/lib/api/schemas/users';
import { UserService } from '@/lib/api/services/users';

// Nostr extension interface
interface NostrWindow extends Window {
  nostr: {
    getPublicKey: () => Promise<string>;
  };
}

// Define the shape of our auth context state
type AuthContextType = {
  user: User | null;
  isLoading: boolean;
  error: Error | null;
  isAuthenticated: boolean;
  nostrPublicKey: string | null;
  signin: () => Promise<void>;
  signout: () => void;
  connectNostr: () => Promise<string | null>;
};

// Create the auth context with default values
const AuthContext = createContext<AuthContextType>({
  user: null,
  isLoading: false,
  error: null,
  isAuthenticated: false,
  nostrPublicKey: null,
  signin: async () => {},
  signout: () => {},
  connectNostr: async () => null,
});

// Auth context provider component
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);
  const [nostrPublicKey, setNostrPublicKey] = useState<string | null>(null);

  // useEffect(() => {
  //   const setupAuthState = async () => {
  //     try {
  //       const savedUser = localStorage.getItem('auth_user');
  //       if (savedUser) {
  //         setUser(JSON.parse(savedUser));
  //       } else {
  //         try {
  //           const demoUser = await UserService.getUser('1');
  //           if (demoUser) {
  //             setUser(demoUser);
  //             localStorage.setItem('auth_user', JSON.stringify(demoUser));
  //           }
  //         } catch (error) {
  //           console.error('Error getting demo user:', error);
  //         }
  //       }

  //       const savedPublicKey = localStorage.getItem('nostr_public_key');
  //       if (savedPublicKey) {
  //         setNostrPublicKey(savedPublicKey);
  //       }
  //     } catch (error) {
  //       console.error('Error restoring auth state:', error);
  //     } finally {
  //       setIsLoading(false);
  //     }
  //   };

  //   setupAuthState();
  // }, []);

  // Connect to Nostr and get public key
  const connectNostr = async (): Promise<string | null> => {
    try {
      // Check if window.nostr exists
      if (typeof window !== 'undefined' && 'nostr' in window) {
        // Request public key from extension
        const publicKey = await (window as NostrWindow).nostr.getPublicKey();

        // Store the public key
        setNostrPublicKey(publicKey);
        localStorage.setItem('nostr_public_key', publicKey);

        return publicKey;
      } else {
        console.error('Nostr extension not found');
        return null;
      }
    } catch (error) {
      console.error('Error connecting to Nostr:', error);
      return null;
    }
  };

  // Signin function that returns the admin user for now
  const signin = async () => {
    setIsLoading(true);
    setError(null);

    try {
      // For now, just return the admin user (id: 1) regardless of credentials
      const adminUser = await UserService.getUser('1');

      if (!adminUser) {
        throw new Error('Admin user not found');
      }

      // Store user in state and localStorage
      setUser(adminUser);
      localStorage.setItem('auth_user', JSON.stringify(adminUser));

      // Try to connect to Nostr if not already connected
      if (!nostrPublicKey) {
        await connectNostr();
      }
    } catch (error) {
      setError(error instanceof Error ? error : new Error('Sign in failed'));
      throw error;
    } finally {
      setIsLoading(false);
    }
  };

  // Sign out function
  const signout = () => {
    setUser(null);
    localStorage.removeItem('auth_user');
    // We don't clear the Nostr public key on logout to maintain the connection
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        isLoading,
        error,
        isAuthenticated: true, // Always set to true to bypass auth checks
        nostrPublicKey,
        signin,
        signout,
        connectNostr,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// Custom hook to use the auth context
export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
