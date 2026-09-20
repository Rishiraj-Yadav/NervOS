import { apiRequest, apiRequestNoContent } from "./client";

export type TriggerKind = "one_time" | "interval" | "cron" | "webhook" | "event";
export interface TriggerSummary { id:number; agent_instance_id:number; kind:TriggerKind; display_name:string; enabled:boolean; config_revision:number; next_fire_at:string|null; event_type:string|null; webhook_path:string|null; created_at:string; updated_at:string; }
export interface Trigger extends TriggerSummary { input_text:string; run_at:string|null; interval_seconds:number|null; cron_expression:string|null; timezone:string|null; secret_created_at:string|null; }
export interface TriggerPage { items:TriggerSummary[]; next_before_id:number|null; }
export interface Occurrence { id:number; trigger_definition_id:number; trigger_revision:number; status:string; run_id:number|null; skip_code:string|null; nominal_at:string|null; event_id:string|null; payload_bytes:number|null; occurred_at:string; created_at:string; }
export interface OccurrencePage { items:Occurrence[]; next_before_id:number|null; }
export interface IssuedWebhook { trigger:Trigger; secret:string }
const isObject=(v:unknown):v is Record<string,unknown>=>typeof v === "object"&&v!==null;
const isTrigger=(v:unknown):v is Trigger=>isObject(v)&&typeof v.id==="number"&&typeof v.kind==="string"&&typeof v.display_name==="string";
const isPage=(v:unknown):v is TriggerPage=>isObject(v)&&Array.isArray(v.items);
const isDetail=(v:unknown):v is Trigger=>isTrigger(v)&&typeof v.input_text==="string";
const isIssued=(v:unknown):v is IssuedWebhook=>isObject(v)&&isDetail(v.trigger)&&typeof v.secret==="string";
const isOccurrencePage=(v:unknown):v is OccurrencePage=>isObject(v)&&Array.isArray(v.items);
export type TriggerCreate = {kind:TriggerKind; agent_instance_id:number; display_name:string; input_text:string; enabled:boolean; run_at?:string; interval_seconds?:number; cron_expression?:string; timezone?:string; event_type?:string};
export type TriggerUpdate = {expected_config_revision:number; display_name?:string; input_text?:string; run_at?:string; interval_seconds?:number; cron_expression?:string; timezone?:string; event_type?:string};
export const listTriggers=()=>apiRequest("/triggers",isPage);
export const getTrigger=(id:number)=>apiRequest(`/triggers/${id}`,isDetail);
export const createTrigger=(body:TriggerCreate)=>apiRequest(`/triggers`,(v): v is Trigger | IssuedWebhook=>isIssued(v)||isDetail(v),{method:"POST",body});
export const updateTrigger=(id:number,body:TriggerUpdate)=>apiRequest(`/triggers/${id}`,isDetail,{method:"PATCH",body});
export const setTriggerEnabled=(id:number,enabled:boolean)=>apiRequest(`/triggers/${id}/${enabled?"enable":"disable"}`,isDetail,{method:"POST"});
const isDeleted=(v:unknown):v is {deleted:true}=>isObject(v)&&v.deleted===true;
export const deleteTrigger=(id:number)=>apiRequest(`/triggers/${id}`,isDeleted,{method:"DELETE"});
export const listOccurrences=(id:number)=>apiRequest(`/triggers/${id}/occurrences`,isOccurrencePage);
export const rotateWebhookSecret=(id:number)=>apiRequest(`/triggers/${id}/rotate-secret`,isIssued,{method:"POST"});
export function isIssuedWebhook(v:Trigger|IssuedWebhook):v is IssuedWebhook{return isIssued(v);}
void apiRequestNoContent;
