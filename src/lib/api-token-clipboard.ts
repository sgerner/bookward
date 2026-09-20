export type ClipboardWriter = {
  writeText: (text: string) => Promise<void>;
};

export const COPY_SUCCESS_MESSAGE = "Copied to clipboard.";
export const COPY_FAILURE_MESSAGE =
  "Copy failed. Select the token above and copy it manually.";

export async function copyApiTokenText(
  token: string,
  clipboard: ClipboardWriter | undefined,
) {
  if (!token || !clipboard) return COPY_FAILURE_MESSAGE;
  try {
    await clipboard.writeText(token);
    return COPY_SUCCESS_MESSAGE;
  } catch {
    return COPY_FAILURE_MESSAGE;
  }
}
