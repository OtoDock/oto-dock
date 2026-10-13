import { QueryClient } from '@tanstack/react-query'

// The app's one query client, made here rather than in main.tsx so code
// outside the component tree (the deep-link handler completing a connect on
// a recreated page) can refresh a query; the tree gets it through the
// provider as before.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 1,
    },
  },
})
