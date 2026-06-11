import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CollectorUrlConfig } from "./CollectorUrlConfig";

afterEach(() => {
  cleanup();
});

describe("CollectorUrlConfig", () => {
  it("renders the current url in display mode", () => {
    render(<CollectorUrlConfig url="http://localhost:8000" onChange={() => {}} />);
    expect(screen.getByTestId("collector-url-display").textContent).toBe(
      "http://localhost:8000",
    );
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("switches to edit mode when Edit is clicked and seeds the input", () => {
    render(<CollectorUrlConfig url="http://localhost:8000" onChange={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    expect(input.value).toBe("http://localhost:8000");
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("calls onChange and exits edit mode on Save", () => {
    const onChange = vi.fn();
    render(
      <CollectorUrlConfig url="http://localhost:8000" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "http://remote.example/" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onChange).toHaveBeenCalledWith("http://remote.example/");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("submits on Enter inside the input", () => {
    const onChange = vi.fn();
    render(
      <CollectorUrlConfig url="http://localhost:8000" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "http://typed.example/" } });
    fireEvent.submit(input.closest("form")!);

    expect(onChange).toHaveBeenCalledWith("http://typed.example/");
  });

  it("trims whitespace before submitting", () => {
    const onChange = vi.fn();
    render(
      <CollectorUrlConfig url="http://localhost:8000" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  http://spaced.example/  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onChange).toHaveBeenCalledWith("http://spaced.example/");
  });

  it("ignores empty submissions and stays in edit mode", () => {
    const onChange = vi.fn();
    render(
      <CollectorUrlConfig url="http://localhost:8000" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("Cancel exits edit mode without calling onChange", () => {
    const onChange = vi.fn();
    render(
      <CollectorUrlConfig url="http://localhost:8000" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "http://discarded.example/" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.getByTestId("collector-url-display").textContent).toBe(
      "http://localhost:8000",
    );
  });

  it("Escape exits edit mode without calling onChange", () => {
    const onChange = vi.fn();
    render(
      <CollectorUrlConfig url="http://localhost:8000" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    fireEvent.keyDown(input, { key: "Escape" });

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("reseeds the input from the latest `url` prop when re-entering edit mode", () => {
    const { rerender } = render(
      <CollectorUrlConfig url="http://a.example/" onChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    rerender(
      <CollectorUrlConfig url="http://b.example/" onChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("Collector") as HTMLInputElement;
    expect(input.value).toBe("http://b.example/");
  });
});
