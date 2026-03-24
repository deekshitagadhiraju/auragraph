import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import remarkGfm from 'remark-gfm';
import rehypeKatex from 'rehype-katex';
import { ArrowLeft } from 'lucide-react';

export default function QuizAttemptReviewPage({ attempt, onBack }) {
    if (!attempt) return null;

    const questions = Array.isArray(attempt.questions) ? attempt.questions : [];
    const responses = Array.isArray(attempt.responses) ? attempt.responses : [];
    const responseByIndex = new Map(
        responses
            .filter(r => r && Number.isInteger(r.question_index))
            .map(r => [r.question_index, (r.selected_option || '').toUpperCase()])
    );

    const score = Number(attempt.correct_answers ?? attempt.score ?? 0);
    const total = Number(attempt.total_questions || questions.length || 0);
    const pct = total > 0 ? Math.round((score / total) * 100) : 0;

    return (
        <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 12, overflow: 'hidden' }}>
            <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <div>
                    <div style={{ fontSize: 16, fontWeight: 800, color: 'var(--text)' }}>
                        {(attempt.test_type || 'quiz').toString().toUpperCase()} Attempt Review
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text3)', marginTop: 2 }}>
                        {new Date(attempt.completed_at || attempt.created_at).toLocaleString()} · {score}/{total} ({pct}%)
                    </div>
                </div>
                <button className="btn btn-ghost btn-sm" onClick={onBack} style={{ gap: 5 }}>
                    <ArrowLeft size={13} /> Back To History
                </button>
            </div>

            <div style={{ maxHeight: '66vh', overflowY: 'auto', padding: '14px 16px' }}>
                {questions.length === 0 ? (
                    <div style={{ fontSize: 13, color: 'var(--text3)' }}>No question data stored for this attempt.</div>
                ) : questions.map((q, idx) => {
                    const correct = (q?.correct || '').toUpperCase();
                    const selected = (responseByIndex.get(idx) || '').toUpperCase();

                    return (
                        <div key={idx} style={{ marginBottom: 20, paddingBottom: 16, borderBottom: idx !== questions.length - 1 ? '1px solid var(--border)' : 'none' }}>
                            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text)', marginBottom: 8 }}>
                                Q{idx + 1}. 
                            </div>
                            <div style={{ fontSize: 14, lineHeight: 1.65, color: 'var(--text)', marginBottom: 10 }}>
                                <ReactMarkdown
                                    remarkPlugins={[remarkMath, remarkGfm]}
                                    rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: false }]]}
                                >
                                    {q?.question || ''}
                                </ReactMarkdown>
                            </div>

                            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                                {['A', 'B', 'C', 'D'].map(opt => {
                                    const isCorrect = opt === correct;
                                    const isSelected = opt === selected;

                                    let bg = 'var(--surface)';
                                    let border = '1px solid var(--border)';
                                    let color = 'var(--text2)';

                                    if (isCorrect) {
                                        bg = '#DCFCE7';
                                        border = '1px solid #86EFAC';
                                        color = '#065F46';
                                    }
                                    if (isSelected && !isCorrect) {
                                        bg = '#FEF2F2';
                                        border = '1px solid #FCA5A5';
                                        color = '#991B1B';
                                    }

                                    return (
                                        <div key={opt} style={{ borderRadius: 8, padding: '9px 11px', background: bg, border, color, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                                            <span style={{ minWidth: 18, fontWeight: 800 }}>{opt})</span>
                                            <div style={{ flex: 1, fontSize: 13, lineHeight: 1.55 }}>
                                                <ReactMarkdown
                                                    remarkPlugins={[remarkMath, remarkGfm]}
                                                    rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: false }]]}
                                                >
                                                    {q?.options?.[opt] || ''}
                                                </ReactMarkdown>
                                            </div>
                                            {isCorrect && <span style={{ fontSize: 11, fontWeight: 700 }}>Correct</span>}
                                            {isSelected && !isCorrect && <span style={{ fontSize: 11, fontWeight: 700 }}>Your Answer</span>}
                                        </div>
                                    );
                                })}
                            </div>

                            <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text3)' }}>
                                Your answer: <b>{selected || 'Not recorded'}</b> · Correct: <b>{correct || '-'}</b>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
