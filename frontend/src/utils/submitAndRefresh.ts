/** Submit a mutation, then refresh its view without confusing refresh failures for submission failures. */
export async function submitAndRefresh<T>(
  submit: () => Promise<T>,
  refresh: (submission: T) => Promise<void>,
): Promise<{ submission: T; refreshed: boolean }> {
  const submission = await submit()
  try {
    await refresh(submission)
    return { submission, refreshed: true }
  } catch {
    return { submission, refreshed: false }
  }
}
