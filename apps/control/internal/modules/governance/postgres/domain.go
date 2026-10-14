package postgres

import "github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"

type ModelProfile = governance.ModelProfile
type RouteTarget = governance.RouteTarget
type FrozenRoute = governance.FrozenRoute
type FrozenRoutes = governance.FrozenRoutes
type PromptMessage = governance.PromptMessage
type PromptVersion = governance.PromptVersion
type PromptPackEntry = governance.PromptPackEntry
type PromptPackVersion = governance.PromptPackVersion
type FrozenPromptBinding = governance.FrozenPromptBinding
type CreationPolicyVersion = governance.CreationPolicyVersion
type VideoModel = governance.VideoModel
type VideoCapabilities = governance.VideoCapabilities
type ImageModel = governance.ImageModel
type ImageCapabilities = governance.ImageCapabilities
type SpeechModel = governance.SpeechModel
type SpeechCapabilities = governance.SpeechCapabilities
type MusicModel = governance.MusicModel
type MusicCapabilities = governance.MusicCapabilities
type SoundEffectModel = governance.SoundEffectModel
type SoundEffectCapabilities = governance.SoundEffectCapabilities
type modelProfileWrite = governance.ModelProfileWrite

var promptInvalid = governance.PromptInvalid
var canonicalPromptJSON = governance.CanonicalJSON
var validateResourcePayload = governance.ValidateResourcePayload
var regexpResourceKey = governance.RegexpResourceKey
var providerURL = governance.ProviderURL
var validateModelProfile = governance.ValidateModelProfile
var modelContains = governance.ModelContains
var routeUsesProfile = governance.RouteUsesProfile
var validateVideoModels = governance.ValidateVideoModels
var videoModelCapabilities = governance.VideoModelCapabilities
var validateImageModels = governance.ValidateImageModels
var imageModelCapabilities = governance.ImageModelCapabilities
var validateSpeechModels = governance.ValidateSpeechModels
var speechModelCapabilities = governance.SpeechModelCapabilities
var validateMusicModels = governance.ValidateMusicModels
var musicModelCapabilities = governance.MusicModelCapabilities
var validateSoundEffectModels = governance.ValidateSoundEffectModels
var soundEffectModelCapabilities = governance.SoundEffectModelCapabilities
var validRouteKind = governance.ValidRouteKind
var normalizeGovernancePayload = governance.NormalizePayload
var workflowPromptNodes = governance.WorkflowPromptNodes
var promptHash = governance.PromptHash
var shaPromptBytes = governance.ShaPromptBytes
var verifyPromptHash = governance.VerifyPromptHash
var compilePromptSchema = governance.CompilePromptSchema
var validatePromptContract = governance.ValidatePromptContract
var renderPromptPreview = governance.RenderPromptPreview
var creationPolicyHash = governance.CreationPolicyHash
var verifyCreationPolicy = governance.VerifyCreationPolicy
var validateCreationPolicy = governance.ValidateCreationPolicy
