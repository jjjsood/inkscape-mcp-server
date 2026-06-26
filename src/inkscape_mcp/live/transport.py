"""`LiveTransport` interface + shared models/errors.

The single semantic interface every live backend implements (architecture §4.4). It is
transport-agnostic: the same tool surface works whether the host uses the extension-socket
bridge or the DBus fast-path. The surface is a FIXED set of read-only semantic
methods — there is no method that accepts arbitrary code or a raw Inkscape Action string
(ADR-003). Backends declare which `LiveCommand`s they support via `supported_commands`; an
unsupported op raises `LiveCapabilityUnsupported` rather than faking a result.

The live gate is ON by default (X1, operator decision 2026-06-14); nothing here runs until a tool
explicitly connects. Every failure
mode is a typed `LiveError` carrying a stable, host-path-free message so the tool/resource layer
can map it cleanly and a live fault never escapes as an opaque traceback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings
from inkscape_mcp.document.inspect import ObjectInfo
from inkscape_mcp.live.protocol import LiveCommand


class LiveError(Exception):
    """Base class for all live-mode failures (stable, host-path-free public message)."""


class LiveNotAvailable(LiveError):
    """No live session is connected, or no transport is available on this host."""


class LiveDisabled(LiveError):
    """Live mode is disabled by configuration (master gate off — X1)."""


class LiveConnectionError(LiveError):
    """Connecting to / communicating with the running instance failed."""


class LiveCapabilityUnsupported(LiveError):
    """The active transport cannot perform this semantic operation (e.g. DBus reads)."""


# --- Shared read models -----------------------------------------------------


class LiveDocumentRef(BaseModel):
    """Identity of the document open in the live instance.

    `path` / `name` are the running Inkscape's own document references (the user's file, which
    they explicitly opted to expose by connecting) — NOT a server-sandbox path. May be null for
    an unsaved document.
    """

    name: str | None = Field(default=None, description="Document title / base filename.")
    path: str | None = Field(default=None, description="On-disk path as reported by Inkscape.")
    object_count: int | None = Field(default=None, description="Object node count, if reported.")


class LiveSelection(BaseModel):
    """The current selection in the live instance, as object ids."""

    object_ids: list[str] = Field(default_factory=list, description="Selected object ids.")
    count: int = Field(default=0, description="Number of selected objects.")


class LiveSelectionInspection(BaseModel):
    """Per-object detail for the selection, reusing the headless `ObjectInfo` shape."""

    objects: list[ObjectInfo] = Field(default_factory=list)
    count: int = Field(default=0)


class LiveMutationResult(BaseModel):
    """Outcome of one semantic live WRITE command, as reported by the backend.

    Carries only object ids and a short human summary — never raw markup or host detail. A
    backend executes the change as an undo-friendly Inkscape step where it can and reports that
    via `undo_friendly`.
    """

    affected_ids: list[str] = Field(
        default_factory=list, description="Object ids the mutation created or changed."
    )
    count: int = Field(default=0, description="Number of objects affected.")
    detail: str = Field(default="", description="Short human-readable summary of the change.")
    undo_friendly: bool = Field(
        default=False, description="Whether the backend applied this as an undoable Inkscape step."
    )


class LiveViewportResult(BaseModel):
    """Outcome of a view-only `set_viewport` op — what the canvas now shows.

    View-only: carries no document change, only the mode the backend applied. There is no markup
    or host detail here.
    """

    mode: str = Field(description="Viewport mode applied (zoom/pan/fit_selection/fit_page).")
    applied: bool = Field(default=True, description="Whether the backend applied the viewport op.")
    detail: str = Field(default="", description="Short human-readable summary.")


class RenderRegion(BaseModel):
    """A validated render region in user units: origin + positive extent.

    Numerics are finite and bounded BEFORE they cross the transport (server-validated in
    ``live/edit.py``); the helper re-checks them defensively. View-only — no document mutation.
    """

    x: float = Field(description="Region origin X in user units.")
    y: float = Field(description="Region origin Y in user units.")
    width: float = Field(gt=0, description="Region width in user units (>0).")
    height: float = Field(gt=0, description="Region height in user units (>0).")


# --- Structured perception models -----------------------------------


class BBox(BaseModel):
    """An axis-aligned bounding box in user units (origin + extent)."""

    x: float = Field(description="Box origin X in user units.")
    y: float = Field(description="Box origin Y in user units.")
    width: float = Field(description="Box width in user units.")
    height: float = Field(description="Box height in user units.")


class SceneSelectionItem(BaseModel):
    """One selected object: its id plus its bounding box (null when the backend can't report it)."""

    id: str = Field(description="Selected object id.")
    bbox: BBox | None = Field(
        default=None, description="Object bounding box in user units, if known."
    )


class SceneViewport(BaseModel):
    """What the live canvas currently shows: zoom factor, center, and the visible region.

    All fields are optional: a backend that cannot report a precise viewport (e.g. an `inkex`
    effect extension running on a document snapshot) leaves them null rather than fabricating
    values. View-only — carries no document state.
    """

    zoom: float | None = Field(default=None, description="Canvas zoom factor, if reported.")
    center: tuple[float, float] | None = Field(
        default=None, description="Viewport center (x, y) in user units, if reported."
    )
    visible_region: BBox | None = Field(
        default=None, description="Visible canvas region in user units, if reported."
    )


class SceneCanvas(BaseModel):
    """The document canvas/page size in user units (plus the declared unit, if any)."""

    width: float | None = Field(default=None, description="Canvas width in user units, if known.")
    height: float | None = Field(default=None, description="Canvas height in user units, if known.")
    units: str | None = Field(
        default=None, description="Declared document unit (e.g. mm/px), if any."
    )


class LiveScene(BaseModel):
    """Machine-readable snapshot of the live canvas paired with each frame.

    Carries the active-document identity, the selection (ids + bboxes), the viewport
    (zoom/center/visible region), the canvas size, and a compact summary of the visible objects.
    The ``visible_objects`` list REUSES the headless ``ObjectInfo`` shape (no parallel model). This
    is pure read-only perception — building it mutates nothing and records nothing.
    """

    active_document: LiveDocumentRef | None = Field(
        default=None, description="Identity of the document open in the live instance."
    )
    selection: list[SceneSelectionItem] = Field(
        default_factory=list, description="Current selection: object ids + bounding boxes."
    )
    selection_count: int = Field(default=0, description="Number of selected objects.")
    viewport: SceneViewport = Field(
        default_factory=SceneViewport, description="Current viewport (zoom/center/visible region)."
    )
    canvas: SceneCanvas = Field(
        default_factory=SceneCanvas, description="Document canvas/page size in user units."
    )
    visible_objects: list[ObjectInfo] = Field(
        default_factory=list,
        description="Compact summary of visible objects (headless ObjectInfo shape).",
    )
    object_count: int = Field(default=0, description="Number of summarized visible objects.")


# --- Change-detection models ----------------------------------------


class LiveStateToken(BaseModel):
    """A CHEAP, server-hashed marker of the live state for change detection.

    Pulled over the fixed ``get_state_token`` command (protocol v5): a small revision marker plus
    the selection ids and a coarse viewport — NEVER the full document or a PNG, so polling it is
    cheap and never busy-renders. The server hashes the three components into stable digests so a
    delta in any one is detectable without serializing anything large. Read-only — building a token
    mutates nothing and records nothing.
    """

    revision: str = Field(
        default="", description="Stable digest of the document revision marker (content/undo)."
    )
    selection: str = Field(default="", description="Stable digest of the ordered selection ids.")
    viewport: str = Field(default="", description="Stable digest of the coarse viewport state.")

    def combined(self) -> str:
        """A single digest of all three components (the whole-state fingerprint)."""
        return f"{self.revision}:{self.selection}:{self.viewport}"


class LiveChange(BaseModel):
    """A classified delta between two `LiveStateToken`s.

    More than one flag may fire at once (e.g. a selection change that also moved the viewport).
    ``changed`` is true when ANY component differs. Carries the new token so a caller can chain the
    next wait without re-reading, and the current selection ids for convenience. Read-only — no
    document mutation, no Operation Record.
    """

    changed: bool = Field(default=False, description="Whether any tracked component changed.")
    selection_changed: bool = Field(default=False, description="Selection ids changed.")
    document_changed: bool = Field(default=False, description="Document revision marker changed.")
    viewport_changed: bool = Field(default=False, description="Viewport (zoom/center) changed.")
    timed_out: bool = Field(
        default=False, description="True when a bounded wait elapsed with no change."
    )
    token: LiveStateToken = Field(
        default_factory=LiveStateToken, description="The newest observed state token."
    )
    selection_ids: list[str] = Field(
        default_factory=list, description="Current selection object ids at observation time."
    )


class TransportProbe(BaseModel):
    """Per-transport availability result (`check_live_support` / ranking input)."""

    name: str = Field(description="Stable transport identifier.")
    available: bool = Field(description="Whether this transport is usable on this host right now.")
    rank: int = Field(description="Selection priority; higher wins among capable transports.")
    supported_commands: list[str] = Field(
        default_factory=list, description="Semantic commands this transport can serve."
    )
    no_freeze: bool = Field(
        default=False,
        description=(
            "Whether driving this transport leaves the Inkscape GUI responsive (no modal freeze). "
            "True only for the Linux DBus action path; the modal socket bridge is False."
        ),
    )
    detail: str = Field(default="", description="Human-readable availability explanation.")


# --- Interface --------------------------------------------------------------


class LiveTransport(ABC):
    """Semantic interface for controlling/reading a running Inkscape instance.

    Subclasses set the three class attributes and implement the lifecycle + read methods. The
    read methods are the ONLY way data crosses the boundary; none takes code or a raw Action
    string. A backend that cannot serve a given op raises `LiveCapabilityUnsupported`.
    """

    #: Stable transport identifier (e.g. "extension-socket", "dbus").
    name: ClassVar[str]
    #: Selection priority among *capable* transports (higher wins).
    rank: ClassVar[int]
    #: The semantic commands this backend can serve (capability set for ranking/selection).
    supported_commands: ClassVar[frozenset[LiveCommand]]
    #: Whether driving this transport leaves the Inkscape GUI responsive. True only for the
    #: Linux DBus action path, which runs ops in Inkscape's own GLib main loop; the modal
    #: effect-extension socket bridge freezes the GUI for the whole session, so it is False.
    no_freeze: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def probe(cls, settings: Settings) -> TransportProbe:
        """Report whether this transport is available on this host (no connection side effects)."""

    @abstractmethod
    def connect(self) -> None:
        """Establish the session. Raises `LiveConnectionError` on failure."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear the session down. Must be idempotent and never raise."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether a usable session is currently established."""

    @abstractmethod
    def get_active_document(self) -> LiveDocumentRef:
        """Identify the document open in the live instance."""

    @abstractmethod
    def get_selection(self) -> LiveSelection:
        """Read the current selection as object ids."""

    @abstractmethod
    def inspect_selection(self) -> LiveSelectionInspection:
        """Inspect the selected objects (semantic, by id) — read-only."""

    @abstractmethod
    def get_document_svg(self) -> str:
        """Return the serialized SVG of the active document (used by sync-to-workspace)."""

    @abstractmethod
    def render_view(self, region: RenderRegion | None = None, scale: float | None = None) -> bytes:
        """Return a rasterized PNG of the live canvas (visual feedback).

        With `region` the renderer clips to that user-unit bbox; with `scale` (>0) it
        downscales/upscales the raster. Both are server-validated before they cross the boundary.
        When neither is given the whole canvas is rendered (backward-compatible).
        """

    # --- Semantic WRITE surface -----------------------------------------
    #
    # Concrete defaults, NOT abstract: a backend that cannot mutate (e.g. DBus action-only)
    # inherits the "unsupported" behaviour, so adding a write command never forces every
    # backend to grow a method. A write-capable backend overrides these AND lists the command
    # in `supported_commands`. None of these accept code or a raw Action string (ADR-003).

    def apply_to_selection(
        self, *, style: dict[str, str], transform: str | None
    ) -> LiveMutationResult:
        """Apply a validated style-property map and/or a composed `transform` to the selection."""
        raise LiveCapabilityUnsupported("this live transport cannot apply edits to the selection")

    def insert_svg(self, svg_fragment: str) -> LiveMutationResult:
        """Insert a safe-parsed SVG fragment into the running document."""
        raise LiveCapabilityUnsupported("this live transport cannot insert SVG")

    def set_selected_text(self, text: str) -> LiveMutationResult:
        """Replace the selected text object's content with `text`."""
        raise LiveCapabilityUnsupported("this live transport cannot edit selected text")

    def export_selection(self) -> bytes:
        """Return a rasterized PNG of just the current selection."""
        raise LiveCapabilityUnsupported("this live transport cannot export the selection")

    # --- View-only surface ----------------------------------------------
    #
    # Concrete defaults like the WRITE surface, but VIEW-only: no document mutation, no Operation
    # Record. A backend that can drive the canvas viewport overrides `set_viewport` AND lists the
    # command in `supported_commands`. The parameters are fixed semantic verbs + bounded numerics;
    # no code or raw Action string ever crosses (ADR-003).

    def set_viewport(
        self,
        *,
        mode: str,
        zoom: float | None = None,
        center: tuple[float, float] | None = None,
        dx: float | None = None,
        dy: float | None = None,
    ) -> LiveViewportResult:
        """Control the live canvas viewport (zoom / pan / fit-to-selection / fit-to-page)."""
        raise LiveCapabilityUnsupported("this live transport cannot control the viewport")

    def get_scene(self) -> LiveScene:
        """Return the structured `LiveScene` for the current frame (read-only perception).

        Active-doc ref, selection ids + bboxes, viewport (zoom/center/visible region), canvas size,
        and a compact ``ObjectInfo`` summary of visible objects. Concrete default like the rest of
        the view-only surface: a backend that cannot serve it inherits the refusal. Read-only — no
        document mutation, no Operation Record.
        """
        raise LiveCapabilityUnsupported("this live transport cannot report a structured scene")

    def get_state_token(self) -> tuple[LiveStateToken, list[str]]:
        """Return a CHEAP, server-hashed `LiveStateToken` (+ selection ids) for change detection.

        Pulls only a small revision marker + selection ids + coarse viewport over the fixed
        ``get_state_token`` command (never the full document or a PNG) and hashes the components, so
        a change-wait can poll it without busy-rendering. Returns the hashed token plus the (plain)
        selection ids carried for the `LiveChange` convenience. Concrete default like the rest of
        the view-only surface: a backend that cannot serve it inherits the refusal. Read-only — no
        document mutation, no Operation Record.
        """
        raise LiveCapabilityUnsupported("this live transport cannot report a state token")

    def supports(self, command: LiveCommand) -> bool:
        """Whether this transport can serve `command`."""
        return command in self.supported_commands
