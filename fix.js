const fs = require('fs');
const path = require('path');

const dirPath = path.join(__dirname, 'frontend/src');

const replacements = {
    '>Doubts<': '>doubts<',
    "label: 'Doubts'": "label: 'doubts'",
    "label: 'Knowledge Map'": "label: 'knowledgeMap'",
    '>Knowledge Map<': '>knowledgeMap<',
    '>Quizzes<': '>quizzes<',
    '>Sniper Exam<': '>sniperExam<',
    "label: 'Sniper Examiner'": "label: 'sniperExaminer'",
    '>Sniper Examiner<': '>sniperExaminer<',
    '>Overview<': '>overview<',
    '>Aura<': '>aura<',
    '>Concepts<': '>concepts<',
    '>Mastery<': '>mastery<',
    '>Notes<': '>notes<',
    '>Dashboard<': '>dashboard<',
    "label: 'Notes'": "label: 'notes'",
    "label: 'Quizzes'": "label: 'quizzes'",

    '>Ask Doubt<': '>askDoubt<',
    '>Mutate<': '>mutate<',
    '>Generate<': '>generate<',
    '>Examine<': '>examine<',
    '>Answered<': '>answered<',
    '>Pending<': '>pending<',
    '>Failed<': '>failed<',
    '>Mutated<': '>mutated<',
    '>Tools<': '>tools<',
    '>Storage<': '>storage<',
    '>CURRENT<': '>current<',
    '>EARNED<': '>earned<',
    '>Knowledge Fusion<': '>knowledgeFusion<',
    
    "textTransform: 'uppercase'": "textTransform: 'none'",
    'textTransform: "uppercase"': "textTransform: 'none'",
    "textTransform: 'capitalize'": "textTransform: 'none'",
    'textTransform: "capitalize"': "textTransform: 'none'"
};

function walkSync(currentDirPath, callback) {
    fs.readdirSync(currentDirPath).forEach((name) => {
        const filePath = path.join(currentDirPath, name);
        const stat = fs.statSync(filePath);
        if (stat.isFile()) {
            callback(filePath, stat);
        } else if (stat.isDirectory()) {
            walkSync(filePath, callback);
        }
    });
}

walkSync(dirPath, (filePath) => {
    if (filePath.endsWith('.jsx') || filePath.endsWith('.js')) {
        let content = fs.readFileSync(filePath, 'utf8');
        let newContent = content;
        for (const [oldStr, newStr] of Object.entries(replacements)) {
            newContent = newContent.split(oldStr).join(newStr);
        }
        if (content !== newContent) {
            fs.writeFileSync(filePath, newContent, 'utf8');
            console.log('Updated ' + filePath);
        }
    }
});