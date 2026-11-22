export type ClipboardWriter = {
  writeText: (text: string) => Promise<void>;
};

export type ApiTokenCopyStatus = {
  ok: boolean;
  error: boolean;
  role: "status" | "alert";
  politeness: "polite" | "assertive";
  message: string;
};

export const COPY_SUCCESS_MESSAGE = "Copied to clipboard.";
export const COPY_FAILURE_MESSAGE =
  "Copy failed. Select the token above and copy it manually.";

const COPY_SUCCESS_STATUS: ApiTokenCopyStatus = {
  ok: true,
  error: false,
  role: "status",
  politeness: "polite",
  message: COPY_SUCCESS_MESSAGE,
};

const COPY_FAILURE_STATUS: ApiTokenCopyStatus = {
  ok: false,
  error: true,
  role: "alert",
  politeness: "assertive",
  message: COPY_FAILURE_MESSAGE,
};

export async function copyApiTokenText(
  token: string,
  clipboard: ClipboardWriter | undefined,
): Promise<ApiTokenCopyStatus> {
  if (!token || !clipboard) return { ...COPY_FAILURE_STATUS };
  try {
    await clipboard.writeText(token);
    return { ...COPY_SUCCESS_STATUS };
  } catch {
    return { ...COPY_FAILURE_STATUS };
  }
}
