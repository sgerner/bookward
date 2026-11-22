import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import BookCoverTrigger from "./BookCoverTrigger.svelte";

describe("server-rendered BookCoverTrigger", () => {
  it("names the cover button with the book title first and view details second while rendering Rank outside the button", () => {
    const { body } = render(BookCoverTrigger, {
      props: {
        id: "cover-42",
        title: "The Left Hand of Darkness",
        score: 94,
        coverUrl: "https://covers.example/left-hand.jpg",
        expanded: true,
      },
    });

    expect(body).not.toContain("aria-label=");
    expect(body).toContain('aria-labelledby="cover-42-title cover-42-action"');
    expect(body).toContain('aria-expanded="true"');
    expect(body).toContain('alt=""');
    expect(body.indexOf("The Left Hand of Darkness")).toBeLessThan(
      body.indexOf("view details"),
    );

    const buttonCloseIndex = body.indexOf("</button>");
    const rankIndex = body.indexOf("Rank 94");
    expect(buttonCloseIndex).toBeGreaterThan(0);
    expect(rankIndex).toBeGreaterThan(buttonCloseIndex);
    expect(body).toContain("pointer-events-none");
  });
});
