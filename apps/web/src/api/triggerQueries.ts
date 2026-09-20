import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createTrigger,
  deleteTrigger,
  getTrigger,
  listOccurrences,
  listTriggers,
  rotateWebhookSecret,
  setTriggerEnabled,
  updateTrigger,
  type Trigger,
  type TriggerCreate,
  type TriggerUpdate,
} from "./triggers";

export const triggerKeys = {
  all: ["triggers"] as const,
  list: () => ["triggers", "list"] as const,
  detail: (id: number) => ["triggers", id] as const,
  history: (id: number) => ["triggers", id, "history"] as const,
};

export const triggersQuery = () => ({ queryKey: triggerKeys.list(), queryFn: listTriggers });
export const triggerQuery = (id: number) => ({ queryKey: triggerKeys.detail(id), queryFn: () => getTrigger(id) });
export const historyQuery = (id: number) => ({ queryKey: triggerKeys.history(id), queryFn: () => listOccurrences(id) });

export function useTriggerActions(onSecret?: (secret: string) => void) {
  const queryClient = useQueryClient();
  const refresh = () => void queryClient.invalidateQueries({ queryKey: triggerKeys.all });
  const safeCreate = async (body: TriggerCreate): Promise<Trigger> => {
    const result = await createTrigger(body);
    if ("secret" in result) {
      onSecret?.(result.secret);
      return result.trigger;
    }
    return result;
  };
  const safeRotate = async (id: number): Promise<Trigger> => {
    const result = await rotateWebhookSecret(id);
    onSecret?.(result.secret);
    return result.trigger;
  };
  return {
    create: useMutation({ mutationFn: safeCreate, onSuccess: refresh }),
    update: useMutation({ mutationFn: ({ id, body }: { id: number; body: TriggerUpdate }) => updateTrigger(id, body), onSuccess: refresh }),
    toggle: useMutation({ mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => setTriggerEnabled(id, enabled), onSuccess: refresh }),
    remove: useMutation({ mutationFn: deleteTrigger, onSuccess: refresh }),
    rotate: useMutation({ mutationFn: safeRotate, onSuccess: refresh }),
  };
}

export { useQuery };
