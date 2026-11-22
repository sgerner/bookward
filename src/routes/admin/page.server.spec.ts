import { afterEach, describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import type { PageData } from "./$types";
import { actions, load } from "./+page.server";
import AdminPage from "./+page.svelte";

afterEach(() => vi.unstubAllGlobals());

describe("admin identity backup recovery hold", () => {
  it("loads identity recovery hold status and requires confirmation before dismissal", async () => {
    const accounts = {
      accounts: [
        {
          id: "admin-1",
          profile_id: "legacy",
          username: "owner",
          display_name: "Owner",
          role: "admin",
          status: "active",
        },
      ],
    };
    const identityBackup = {
      enabled: true,
      interval_hours: 24,
      retention_count: 7,
      recovery_hold: true,
      recovery_hold_at: "2026-11-21T20:00:00+00:00",
      last_success_at: "2026-11-20T19:00:00+00:00",
      last_attempt_at: null,
      last_error: null,
      last_restore_at: null,
      backups: [
        {
          id: "bookward-auth-20261120T190000Z-aaaa1111",
          created_at: "2026-11-20T19:00:00+00:00",
          size_bytes: 32768,
        },
        {
          id: "bookward-auth-20261119T190000Z-bbbb2222",
          created_at: "2026-11-19T19:00:00+00:00",
          size_bytes: 30720,
        },
      ],
    };

    const fetchMock = vi.fn().mockImplementation((input: string) => {
      const payload = input.includes("/admin/auth-backups")
        ? identityBackup
        : accounts;
      return Promise.resolve(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const data = (await load({
      locals: {
        user: {
          id: "admin-1",
          profile_id: "legacy",
          username: "owner",
          display_name: "Owner",
          role: "admin",
          must_change_password: false,
        },
      },
    } as never)) as Omit<PageData, "user">;

    expect(data.identityBackup?.recovery_hold).toBe(true);
    expect(data.identityBackup?.backups[0].id).toBe(
      "bookward-auth-20261120T190000Z-aaaa1111",
    );

    const rendered = render(AdminPage, {
      props: {
        data: {
          ...data,
          user: {
            id: "admin-1",
            profile_id: "legacy",
            username: "owner",
            display_name: "Owner",
            role: "admin",
            must_change_password: false,
          },
        },
        form: null,
      },
    });
    expect(rendered.body).toContain("Recovery hold · backups paused");
    expect(rendered.body).toContain(
      "afterword_engine.auth_admin restore bookward-auth-20261120T190000Z-aaaa1111 --confirm",
    );
    expect(rendered.body).toContain(
      'value="bookward-auth-20261120T190000Z-aaaa1111" selected',
    );
    expect(rendered.body).toContain("?/dismissIdentityHold");

    fetchMock.mockClear();
    const rejected = await actions.dismissIdentityHold!({
      request: new Request("http://afterword.test/admin", {
        method: "POST",
        body: new FormData(),
      }),
    } as never);
    expect(rejected).toMatchObject({ status: 400 });
    expect(fetchMock).not.toHaveBeenCalled();

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ dismissed: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const body = new FormData();
    body.set("confirm", "KEEP");
    await expect(
      actions.dismissIdentityHold!({
        request: new Request("http://afterword.test/admin", {
          method: "POST",
          body,
        }),
      } as never),
    ).resolves.toEqual({
      message:
        "Account-registry recovery hold dismissed. Scheduled backups have resumed.",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/admin/auth-backups/recovery-hold/dismiss",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      }),
    );
  });
});
