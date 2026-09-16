import axios from "axios";
import { API_URL } from "./api";

export interface CutEvidence { pts: number; before_pts: number; before_url: string; after_url: string; source: string }
export interface SourceAnalysis {
  time_base: string; start_pts: number; end_pts: number; frame_pts: number[];
  duration_seconds: number; width: number; height: number; audio_streams: number;
  candidates: CutEvidence[]; contact_sheet_url: string;
}
export interface RecreationProject {
  id: string; title: string; source_url: string; revision: number; analysis_id: string;
  status: "registered" | "queued" | "analyzing" | "review" | "confirmed" | "failed";
  error: string | null; analysis: SourceAnalysis | null;
  timeline: { cuts: { pts: number; source: string }[]; shots: { start_pts: number; end_pts: number }[] } | null;
}

export function seconds(analysis: SourceAnalysis, pts: number): number {
  const [n, d = 1] = analysis.time_base.split("/").map(Number);
  return (pts - analysis.start_pts) * n / d;
}

export function importCuts(text: string, analysis: SourceAnalysis): number[] {
  const times = text.trim().split(/[\s,;]+/).filter(Boolean).map(Number);
  if (times.length > 120 || times.some(t => !Number.isFinite(t) || t <= 0 || t >= analysis.duration_seconds)) {
    throw new Error("invalidTimeline");
  }
  const cuts = times.map(t => {
    const nearest = analysis.frame_pts.slice(1).reduce((a, b) =>
      Math.abs(seconds(analysis, a) - t) <= Math.abs(seconds(analysis, b) - t) ? a : b);
    // Six-decimal timestamp exports may round, but never snap a cut to a different frame.
    if (Math.abs(seconds(analysis, nearest) - t) > 0.00000051) throw new Error("invalidTimeline");
    return nearest;
  });
  if (cuts.some((p, i) => i > 0 && p <= cuts[i - 1])) throw new Error("invalidTimeline");
  return cuts;
}

export const recreationApi = {
  searchMedia: (params: { q?: string; kind?: string; project_id?: string; limit?: number; cursor?: number } = {}) =>
    axios.get(`${API_URL}/recreation/media`, { params }).then(r => r.data),
  list: (): Promise<RecreationProject[]> => axios.get(`${API_URL}/recreation/projects`).then(r => r.data),
  get: (id: string): Promise<RecreationProject> => axios.get(`${API_URL}/recreation/projects/${id}`).then(r => r.data),
  upload: (file: File): Promise<RecreationProject> => {
    const data = new FormData(); data.append("file", file);
    return axios.post(`${API_URL}/recreation/projects`, data).then(r => r.data);
  },
  analyze: (project: RecreationProject): Promise<RecreationProject> =>
    axios.post(`${API_URL}/recreation/projects/${project.id}/analyze`, { revision: project.revision }).then(r => r.data),
  confirm: (project: RecreationProject, cuts: number[]): Promise<RecreationProject> =>
    axios.put(`${API_URL}/recreation/projects/${project.id}/timeline`, {
      revision: project.revision, analysis_id: project.analysis_id, cut_pts: cuts,
    }).then(r => r.data),
  evidence: (project: RecreationProject, pts: number): Promise<CutEvidence> =>
    axios.post(`${API_URL}/recreation/projects/${project.id}/evidence`, { analysis_id: project.analysis_id, pts }).then(r => r.data),
};
