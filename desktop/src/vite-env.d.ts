export {}

declare global {
  interface Window {
    athena: {
      getApiBase: () => Promise<string>
      getWsUrl: (sessionId: string) => Promise<string>
    }
  }
}
