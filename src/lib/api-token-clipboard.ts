export type ClipboardWriter = {
  writeText: (text: string) => Promise<void>;
};

export type ApiTokenCopyStatus = {
  error: boolean;
  message: string;
};

export const COPY_SUCCESS_MESSAGE = "Copied to clipboard.";
export const COPY_FAILURE_MESSAGE =
  "Copy failed. Select the token above and copy it manually.";

export async function copyApiTokenText(
  token: string,
  clipboard: ClipboardWriter | undefined,
): Promise<ApiTokenCopyStatus> {
  if (!token || !clipboard) {
    return { error: true, message: COPY_FAILURE_MESSAGE };
  }
  try {
    await clipboard.writeText(token);
    return { error: false, message: COPY_SUCCESS_MESSAGE };
  } catch {
    return { error: true, message: COPY_FAILURE_MESSAGE };
  }
}
