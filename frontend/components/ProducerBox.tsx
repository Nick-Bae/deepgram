// components/ProducerBox.tsx
"use client";
import { useState } from "react";
import { useTranslationSocket } from "../utils/useTranslationSocket";
import { useSentenceBuffer } from "../utils/useSentenceBuffer";

export default function ProducerBox() {
  const { connected, sendProducerText } = useTranslationSocket({ isProducer: true });
  const [debugBuf, setDebugBuf] = useState("");

  const buffer = useSentenceBuffer(
    (sentence) => {
      // When a full sentence is ready, send to backend as a final commit
      sendProducerText(sentence, "ko", "en", false, undefined, undefined, true);
      setDebugBuf(""); // cleared after sending
    },
    { timeoutMs: 1200, minLength: 4 } // tweak as you like
  );

  // Simulate STT chunks using the input box for now
  const [input, setInput] = useState("");
  const pushChunk = () => {
    if (!input.trim()) return;
    buffer.add(input);
    setDebugBuf(buffer.peek()); // show current buffer
    setInput("");
  };

  const forceFlush = () => buffer.flush(true);

  return (
    <div style={{ display:"grid", gap:8 }}>
      <div>WS: {connected ? "🟢 connected" : "🔴 disconnected"}</div>

      <textarea
        rows={2}
        value={input}
        onChange={e=>setInput(e.target.value)}
        placeholder="Type partial STT chunks… (e.g., '오늘 하나님은', then '사랑이십니다.')"
      />
      <div style={{ display:"flex", gap:8 }}>
        <button onClick={pushChunk}>Add Chunk</button>
        <button onClick={forceFlush}>Force Flush (simulate silence)</button>
      </div>

      <div style={{ fontSize:12, opacity:0.8 }}>
        Buffer: <code>{debugBuf || "—"}</code>
      </div>
    </div>
  );
}
