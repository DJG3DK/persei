import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { PerseiMark } from "./PerseiMark";

describe("<PerseiMark />", () => {
  it("draws the mark with no background of its own", () => {
    // The whole point: it floats. The favicon is a rounded tile with a navy
    // background baked in, which on a dark surface reads as a dark square.
    const { container } = render(<PerseiMark />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("fill")).toBe("none");
    const shapes = svg.querySelectorAll("rect, path, circle");
    expect(shapes.length).toBe(2);                                   // the gem, and its P
    expect(svg.querySelector("rect")!.getAttribute("transform")).toBe("rotate(45 12 12)"); // a diamond, not a plate
  });

  it("keeps the brand colours rather than following the theme", () => {
    // --accent is per-theme (gold / periwinkle / orchid). Following it would
    // recolour the gem to match whichever theme is on — cohesive, and no
    // longer the Persei mark. The sidebar lockup is fixed for the same
    // reason and sits on the same screen.
    const { container } = render(<PerseiMark />);
    const fills = [...container.querySelectorAll("rect, path")].map((p) => p.getAttribute("fill"));
    expect(fills).toEqual(["#EFBF4E", "#0B1426"]);
    expect(fills.some((f) => f?.includes("var("))).toBe(false);
  });

  it("scales without resampling", () => {
    const { container } = render(<PerseiMark size={64} />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe("64");
    // The viewBox is what makes it crisp at any size, including 32px on a
    // retina display, where a 32px PNG is not.
    expect(svg.getAttribute("viewBox")).toBe("0 0 24 24");
  });

  it("is hidden from screen readers", () => {
    // The speaker's name is already beside it in text; "Persei logo, Agent"
    // is worse than "Agent".
    const { container } = render(<PerseiMark />);
    expect(container.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
  });
});
