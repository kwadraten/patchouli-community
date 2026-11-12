open System
open System.Collections.Generic
open System.IO
open System.Net
open System.Net.Http
open System.Text
open System.Text.RegularExpressions
open System.Threading
open System.Threading.Tasks
open Patchouli.Core.Bibliography.MetadataLookup
open Patchouli.Core.Results
open Patchouli.Extensions.Contracts
open Patchouli.Extensions.Sdk

[<Settings>]
type Settings = {
    [<Field("Maximum page body in bytes"); Default(2097152); Range(65536, 33554432)>]
    MaxBodyBytes: int
}

let sourceDefinition =
    MetadataSourceDefinition("url-meta", "URL Meta Tags",
        HashSet<string>([| "url" |], StringComparer.OrdinalIgnoreCase), 80, true)

let private metaTagPattern =
    Regex("<meta\s[^>]*>", RegexOptions.IgnoreCase ||| RegexOptions.Compiled, TimeSpan.FromSeconds(5.0))

let private attributePattern =
    Regex("([A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))",
        RegexOptions.Compiled, TimeSpan.FromSeconds(5.0))

let private titlePattern =
    Regex("<title[^>]*>(.*?)</title>",
        RegexOptions.IgnoreCase ||| RegexOptions.Singleline ||| RegexOptions.Compiled,
        TimeSpan.FromSeconds(5.0))

let private htmlLangPattern =
    Regex("<html[^>]*?\slang\s*=\s*[\"']?([A-Za-z][A-Za-z0-9_-]*)",
        RegexOptions.IgnoreCase ||| RegexOptions.Compiled, TimeSpan.FromSeconds(5.0))

let private metaValues (html: string) =
    let values = Dictionary<string, ResizeArray<string>>(StringComparer.OrdinalIgnoreCase)
    for tagMatch in metaTagPattern.Matches(html) do
        let attributes = Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        for attributeMatch in attributePattern.Matches(tagMatch.Value) do
            let group (index: int) = attributeMatch.Groups.[index]
            let raw =
                if (group 2).Success then (group 2).Value
                elif (group 3).Success then (group 3).Value
                else (group 4).Value
            attributes[(group 1).Value] <- Option.defaultValue "" (Option.ofObj (WebUtility.HtmlDecode(raw)))
        let name =
            match attributes.TryGetValue("name") with
            | true, value -> Some value
            | _ -> match attributes.TryGetValue("property") with
                   | true, value -> Some value
                   | _ -> None
        match name, attributes.TryGetValue("content") with
        | Some key, (true, content) when not (String.IsNullOrWhiteSpace content) ->
            match values.TryGetValue(key) with
            | true, list -> list.Add(content.Trim())
            | _ -> values[key] <- ResizeArray<string>([ content.Trim() ])
        | _ -> ()
    values

let private firstValue (values: IDictionary<string, ResizeArray<string>>) (names: string list) =
    names |> List.tryPick (fun name ->
        match values.TryGetValue(name) with
        | true, list when list.Count > 0 -> Some list.[0]
        | _ -> None)

let private allNamed (values: IDictionary<string, ResizeArray<string>>) (name: string) =
    match values.TryGetValue(name) with
    | true, list -> seq { yield! list }
    | _ -> Seq.empty

let private dateParts (text: string) =
    let parts =
        Regex.Split(text.Trim(), "[^0-9]+")
        |> Array.choose (fun part ->
            match Int32.TryParse(part) with
            | true, value -> Some value
            | _ -> None)
    if parts.Length > 0 && parts[0] >= 1000 && parts[0] <= 9999 then
        Some (Array.truncate 3 parts)
    else
        None

let private issuedDate (text: string) =
    match dateParts text with
    | Some parts -> MetadataDate("issued", parts)
    | None -> MetadataDate("issued", Literal = text)

let private normalizeDoi (value: string) =
    let trimmed = value.Trim()
    match [ "https://doi.org/"; "http://doi.org/"; "doi:" ]
          |> List.tryFind (fun prefix -> trimmed.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) with
    | Some prefix -> trimmed.Substring(prefix.Length).Trim().ToLowerInvariant()
    | None -> trimmed.ToLowerInvariant()

let private languageTag (value: string) =
    let normalized = value.Trim().Replace('_', '-')
    if Regex.IsMatch(normalized, "^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$") then Some normalized else None

let private pageTitle (html: string) =
    let matched = titlePattern.Match(html)
    if matched.Success then
        match Option.ofObj (WebUtility.HtmlDecode(matched.Groups[1].Value)) with
        | Some decoded when decoded.Trim().Length > 0 -> Some (decoded.Trim())
        | _ -> None
    else
        None

let private buildCandidate (pageUrl: string) (html: string)
                           (values: IDictionary<string, ResizeArray<string>>) =
    let identifiers = ResizeArray<MetadataIdentifier>()
    match firstValue values [ "citation_doi" ] with
    | Some doi -> identifiers.Add(MetadataIdentifier("doi", normalizeDoi doi))
    | None -> ()
    match firstValue values [ "citation_isbn" ] with
    | Some isbn ->
        let cleaned = String(isbn |> Seq.filter (fun ch -> Char.IsDigit ch || ch = 'X' || ch = 'x') |> Seq.toArray)
        if cleaned.Length > 0 then
            identifiers.Add(MetadataIdentifier("isbn", cleaned))
    | None -> ()
    match firstValue values [ "citation_issn" ] with
    | Some issn -> identifiers.Add(MetadataIdentifier("issn", issn.Trim()))
    | None -> ()
    identifiers.Add(MetadataIdentifier("url", pageUrl))
    let journal = firstValue values [ "citation_journal_title"; "DC.source"; "og:site_name" ]
    let firstPage = firstValue values [ "citation_firstpage" ]
    let lastPage = firstValue values [ "citation_lastpage" ]
    let pages =
        match firstPage, lastPage with
        | Some first, Some last -> Some(first + "-" + last)
        | Some first, None -> Some first
        | _ -> firstValue values [ "citation_pages" ]
    let creators =
        seq {
            yield! allNamed values "citation_author"
            yield! allNamed values "DC.creator"
            yield! allNamed values "author"
        }
        |> Seq.distinct
        |> Seq.map (fun name -> MetadataCreator("author", Literal = name))
        |> Seq.toArray
    let dates =
        match firstValue values [ "citation_publication_date"; "citation_online_date"
                                  "citation_date"; "article:published_time"; "DC.date" ] with
        | Some text -> [| issuedDate text |]
        | None -> Array.empty
    let language =
        firstValue values [ "citation_language"; "DC.language"; "og:locale" ]
        |> Option.bind languageTag
        |> Option.orElseWith (fun () ->
            let matched = htmlLangPattern.Match(html)
            if matched.Success then languageTag matched.Groups[1].Value else None)
    let tags =
        allNamed values "citation_keywords"
        |> Seq.collect (fun text -> text.Split([| ';'; ',' |], StringSplitOptions.RemoveEmptyEntries))
        |> Seq.map (fun tag -> tag.Trim())
        |> Seq.filter (fun tag -> tag.Length > 0)
        |> Seq.distinct
        |> Seq.toArray
    let isArticle =
        journal.IsSome
        || firstValue values [ "citation_doi"; "citation_volume"; "citation_issue" ] |> Option.isSome
    let title =
        match firstValue values [ "citation_title"; "og:title"; "twitter:title"; "DC.title" ] with
        | Some value -> Some value
        | None -> pageTitle html
    let publicationTitle = Option.toObj journal
    let publisher = firstValue values [ "citation_publisher"; "DC.publisher" ] |> Option.toObj
    let volume = firstValue values [ "citation_volume" ] |> Option.toObj
    let issue = firstValue values [ "citation_issue" ] |> Option.toObj
    let pagesText = Option.toObj pages
    let languageText = Option.toObj language
    let abstractText =
        firstValue values [ "citation_abstract"; "og:description"; "twitter:description"
                            "description"; "DC.description" ] |> Option.toObj
    MetadataCandidate("url-meta",
        Option.toObj title,
        Creators = creators,
        Dates = dates,
        PublicationTitle = publicationTitle,
        Publisher = publisher,
        Volume = volume,
        Issue = issue,
        Pages = pagesText,
        Language = languageText,
        Abstract = abstractText,
        Tags = tags,
        Identifiers = identifiers,
        SuggestedItemType = (if isArticle then "article-journal" else "webpage"),
        TypeConfidence = (if isArticle then 0.85 else 0.5))

let private isEmptyCandidate (candidate: MetadataCandidate) =
    String.IsNullOrWhiteSpace(candidate.Title)
    && (match candidate.Identifiers with
        | null -> true
        | identifiers -> identifiers.Count = 0)

let private fetchPage (settings: Settings) (resources: IExtensionResources) (uri: Uri)
                      (cancellationToken: CancellationToken) = task {
    do! resources.WaitToProceedAsync(cancellationToken)
    use request = new HttpRequestMessage(HttpMethod.Get, uri)
    request.Headers.Accept.ParseAdd("text/html,application/xhtml+xml;q=0.9,*/*;q=0.1")
    try
        use! response = resources.Http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken)
        let statusCode = int response.StatusCode
        if statusCode = 404 || statusCode = 410 then
            return Result<string>.Failure(MetadataLookupErrorCodes.NotFound, "The page was not found.")
        elif statusCode = 429 then
            return Result<string>.Failure(MetadataLookupErrorCodes.RateLimited,
                "The site asked to slow down; retry later.")
        elif statusCode >= 400 then
            return Result<string>.Failure(MetadataLookupErrorCodes.ProviderUnavailable,
                $"The site returned HTTP {statusCode}.")
        elif response.Content.Headers.ContentLength
             |> Option.ofNullable
             |> Option.exists (fun length -> length > int64 settings.MaxBodyBytes) then
            return Result<string>.Failure(MetadataLookupErrorCodes.InvalidResponse,
                "The page exceeds the configured size limit.")
        else
            use! stream = response.Content.ReadAsStreamAsync(cancellationToken)
            use buffer = new MemoryStream()
            let chunk = Array.zeroCreate 8192
            let mutable overLimit = false
            let mutable reading = true
            while reading do
                let! count = stream.ReadAsync(chunk, 0, chunk.Length, cancellationToken)
                if count = 0 then
                    reading <- false
                else
                    buffer.Write(chunk, 0, count)
                    if buffer.Length > int64 settings.MaxBodyBytes then
                        overLimit <- true
                        reading <- false
            if overLimit then
                return Result<string>.Failure(MetadataLookupErrorCodes.InvalidResponse,
                    "The page exceeds the configured size limit.")
            else
                let encoding =
                    try
                        match Option.ofObj response.Content.Headers.ContentType
                              |> Option.bind (fun contentType -> Option.ofObj contentType.CharSet) with
                        | Some charSet -> Encoding.GetEncoding(charSet.Trim(char 34))
                        | None -> Encoding.UTF8
                    with _ -> Encoding.UTF8
                return Result<string>.Success(encoding.GetString(buffer.ToArray()))
    with
    | :? TaskCanceledException when not cancellationToken.IsCancellationRequested ->
        return Result<string>.Failure(MetadataLookupErrorCodes.Timeout, "The page request timed out.")
    | :? OperationCanceledException as cancelled -> return raise cancelled
    | :? HttpRequestException as error ->
        return Result<string>.Failure(MetadataLookupErrorCodes.ProviderUnavailable, error.Message)
    | error ->
        return Result<string>.Failure(MetadataLookupErrorCodes.InvalidResponse, error.Message)
}

let lookup (identifier: NormalizedIdentifier) (settings: Settings)
           (resources: IExtensionResources) (cancellationToken: CancellationToken) = task {
    if identifier.Scheme <> "url" then
        return Result<MetadataCandidate>.Failure(MetadataLookupErrorCodes.UnsupportedIdentifier,
            "The URL meta source only accepts url identifiers.")
    else
        let pageUrl = identifier.Value.Trim()
        let wellFormed, uri = Uri.TryCreate(pageUrl, UriKind.Absolute)
        match Option.ofObj uri with
        | None -> return Result<MetadataCandidate>.Failure(MetadataLookupErrorCodes.UnsupportedIdentifier,
            "The url identifier must be an absolute HTTP(S) URL.")
        | Some pageUri when
            not wellFormed || (pageUri.Scheme <> Uri.UriSchemeHttp && pageUri.Scheme <> Uri.UriSchemeHttps) ->
            return Result<MetadataCandidate>.Failure(MetadataLookupErrorCodes.UnsupportedIdentifier,
                "The url identifier must be an absolute HTTP(S) URL.")
        | Some pageUri ->
            let! bodyResult = fetchPage settings resources pageUri cancellationToken
            if bodyResult.IsFailure then
                return Result<MetadataCandidate>.Failure(
                    Option.defaultValue MetadataLookupErrorCodes.InvalidResponse
                        (Option.ofObj bodyResult.ErrorCode),
                    Option.defaultValue "The metadata source request failed."
                        (Option.ofObj bodyResult.ErrorMessage))
            else
                let candidate = buildCandidate pageUrl bodyResult.Value (metaValues bodyResult.Value)
                if isEmptyCandidate candidate then
                    return Result<MetadataCandidate>.Failure(MetadataLookupErrorCodes.InvalidResponse,
                        "The page did not expose citation or social metadata.")
                else
                    return Result<MetadataCandidate>.Success candidate
}

/// <summary>Fetches an academic web page and converts its Highwire citation_* and Open Graph meta tags into a metadata candidate for url identifiers.</summary>
/// <patchouli-extension id="kwadraten.url-metadata" kind="metadata" version="1.0.0" host-api="patchouli.extensions/4">
///   <name>URL Meta Tags</name>
/// </patchouli-extension>
let extension =
    Extension.createMetadata<Settings> sourceDefinition (fun settings identifier resources token ->
        lookup identifier settings resources token)
