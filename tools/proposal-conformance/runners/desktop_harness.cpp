// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
// Drive the desktop parser (shell/agentproposalparser.cpp) on the agent-proposal fixtures.
// Build with runners/build_desktop.sh (into /tmp); writes nothing outside its output file.
// UTF-16 offsets are converted to code points.
#include "shell/agentproposalparser.h"

#include <QCoreApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

using namespace Thoughts;

static int codePoints(const QString &source, int offset)
{
    return static_cast<int>(source.left(offset).toUcs4().size());
}

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    if (argc != 3) return 2;
    QFile input(QString::fromLocal8Bit(argv[1]));
    if (!input.open(QIODevice::ReadOnly)) return 3;
    const QJsonObject corpus = QJsonDocument::fromJson(input.readAll()).object();
    QJsonObject results;
    for (const auto &value : corpus.value(QStringLiteral("cases")).toArray()) {
        const QJsonObject item = value.toObject();
        const QString path = item.value(QStringLiteral("path")).toString();
        const QString source = item.value(QStringLiteral("source")).toString();
        QVector<AgentProposedEdit> proposals;
        QString error;
        QJsonArray list, issues;
        if (parseAgentProposedEditsForSource(path, source, &proposals, &error)) {
            for (const auto &p : proposals) {
                list.append(QJsonObject{{QStringLiteral("id"), p.id}, {QStringLiteral("created_at"), p.createdAt},
                                        {QStringLiteral("conversation_id"), p.conversationId},
                                        {QStringLiteral("explanation"), p.motivation}, {QStringLiteral("original"), p.before},
                                        {QStringLiteral("replacement"), p.after},
                                        {QStringLiteral("span"), QJsonObject{{QStringLiteral("start"), codePoints(source, p.offset)},
                                                                             {QStringLiteral("end"), codePoints(source, p.offset + p.length)}}}});
            }
        } else {
            issues.append(QJsonObject{{QStringLiteral("message"), error}});
        }
        QJsonArray decisions;
        for (const auto &d : item.value(QStringLiteral("decisions")).toArray()) {
            const QJsonObject decision = d.toObject();
            QStringList ids;
            for (const auto &id : decision.value(QStringLiteral("ids")).toArray()) ids.append(id.toString());
            const QString action = decision.value(QStringLiteral("action")).toString() == QLatin1String("accept") ? QStringLiteral("accept") : QStringLiteral("disregard");
            QString output, decisionError;
            if (transformAgentProposedEditsForSource(path, source, ids, action, &output, &decisionError))
                decisions.append(QJsonObject{{QStringLiteral("output"), output}});
            else
                decisions.append(QJsonObject{{QStringLiteral("refused"), QJsonValue()}, {QStringLiteral("message"), decisionError}});
        }
        results.insert(item.value(QStringLiteral("id")).toString(),
                       QJsonObject{{QStringLiteral("parse"), QJsonObject{{QStringLiteral("proposals"), list}, {QStringLiteral("issues"), issues}}},
                                   {QStringLiteral("decisions"), decisions}});
    }
    QJsonObject writers;
    for (const auto &value : corpus.value(QStringLiteral("writers")).toArray()) {
        const QJsonObject item = value.toObject();
        if (item.value(QStringLiteral("format")).toString() != QLatin1String("markdown")) continue;  // TeX writer is inline in authorProposal
        const QJsonObject fields = item.value(QStringLiteral("fields")).toObject();
        AgentProposedEdit p;
        p.id = fields.value(QStringLiteral("id")).toString();
        p.createdAt = fields.value(QStringLiteral("created_at")).toString();
        p.conversationId = fields.value(QStringLiteral("conversation_id")).toString();
        p.motivation = fields.value(QStringLiteral("explanation")).toString();
        p.before = fields.value(QStringLiteral("original")).toString();
        p.after = fields.value(QStringLiteral("replacement")).toString();
        writers.insert(item.value(QStringLiteral("id")).toString(), formatMarkdownAgentProposal(p));
    }
    const QJsonObject capabilities{{QStringLiteral("spans"), true}, {QStringLiteral("issues"), true}, {QStringLiteral("issue_codes"), false},
                                   {QStringLiteral("decisions"), true}, {QStringLiteral("refusal_codes"), false},
                                   {QStringLiteral("fields"), QJsonArray{QStringLiteral("id"), QStringLiteral("created_at"), QStringLiteral("conversation_id"),
                                                                         QStringLiteral("explanation"), QStringLiteral("original"), QStringLiteral("replacement")}}};
    QFile output(QString::fromLocal8Bit(argv[2]));
    if (!output.open(QIODevice::WriteOnly)) return 4;
    output.write(QJsonDocument(QJsonObject{
        {QStringLiteral("desktop"), QJsonObject{{QStringLiteral("capabilities"), capabilities}, {QStringLiteral("cases"), results},
                                                {QStringLiteral("writers"), writers}}}}).toJson());
    return 0;
}
