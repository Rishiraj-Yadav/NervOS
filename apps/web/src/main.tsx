import "./styles.css";
import App from "./App";
import { createQueryClient } from "./app/queryClient";
import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

const queryClient = createQueryClient();

const root = document.getElementById("root");
if (root === null) {
  throw new Error("NervOS root element is missing.");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
