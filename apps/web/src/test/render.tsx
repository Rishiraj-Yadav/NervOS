import { QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement } from "react";
import {
  createMemoryRouter,
  RouterProvider,
  type RouteObject,
} from "react-router-dom";

import { createQueryClient } from "../app/queryClient";

interface RenderWithRouterOptions extends Omit<RenderOptions, "wrapper"> {
  initialEntries?: string[];
}

export function renderWithRouter(
  routes: RouteObject[],
  { initialEntries = ["/"], ...renderOptions }: RenderWithRouterOptions = {},
) {
  const queryClient = createQueryClient();
  const router = createMemoryRouter(routes, { initialEntries });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
    renderOptions,
  );

  return { ...result, queryClient, router };
}

export function renderWithQueryClient(
  ui: ReactElement,
  renderOptions: Omit<RenderOptions, "wrapper"> = {},
) {
  const queryClient = createQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
    renderOptions,
  );

  return { ...result, queryClient };
}
